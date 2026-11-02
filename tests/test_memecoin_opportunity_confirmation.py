import asyncio
import sqlite3
import inspect
import base64
import json
import struct
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
import websockets
from eth_abi import encode

import scripts.memecoin_opportunity_worker as worker
import sniper.services.memecoin_opportunity_sources as opportunity_sources
from sniper.research.memecoin_opportunity.types import ConfirmationState
from sniper.research.memecoin_opportunity.continuity import (
    record_processed_cursor,
    record_source_failure,
)
from sniper.research.memecoin_opportunity.bags_coverage import BagsLaunch
from sniper.research.memecoin_opportunity.decoders import (
    DecodeBlocker,
    base58_encode,
    blocked,
)
from sniper.research.memecoin_opportunity.store import connect
from scripts.memecoin_opportunity_worker import (
    _evm_poll_range,
    _evm_receipt_confirmation,
    _finalized_evm_height,
    _solana_event_context,
    _indexed_solana_recovery,
    _newest_solana_cursor,
    _pump_recovery_transport,
    _solana_recovery_cursors,
    _solana_confirmation_target,
    _solana_multi_lane,
    _solana_order_from_block,
    _track_pending_confirmation,
    _unique_confirmation_signatures,
)


def test_recovery_cursor_cannot_replace_a_newer_live_solana_cursor():
    live = "102:live-signature:4"
    recovery = "101:recovery-signature:8"

    assert _newest_solana_cursor(live, recovery) == live
    assert _newest_solana_cursor(live, "103:newer-signature:1") == (
        "103:newer-signature:1"
    )
    assert _newest_solana_cursor(live, "102:same-slot-replay:1") == (
        "102:same-slot-replay:1"
    )


def test_indexed_pump_recovery_uses_the_dedicated_recovery_governor():
    primary_governor = object()
    recovery_governor = object()
    settings = SimpleNamespace(
        solana_indexed_recovery=True,
        solana_rpc_url="https://indexed.invalid",
        solana_recovery_rpc_url="https://public.invalid",
    )

    rpc_url, governor, indexed = _pump_recovery_transport(
        settings, primary_governor, recovery_governor
    )

    assert rpc_url == "https://indexed.invalid"
    assert governor is recovery_governor
    assert indexed is True


def test_solana_confirmation_uses_the_paced_recovery_provider():
    primary_governor = object()
    recovery_governor = object()
    settings = SimpleNamespace(
        solana_rpc_url="https://quota-exhausted.invalid",
        solana_recovery_rpc_url="https://public.invalid",
    )

    rpc_url, governor = worker._solana_confirmation_transport(
        settings, primary_governor, recovery_governor
    )

    assert rpc_url == "https://public.invalid"
    assert governor is recovery_governor


def test_confirmation_recovery_does_not_impersonate_transport_heartbeat(monkeypatch):
    class Conn:
        blocker_code = None

        def execute(self, _sql, _params):
            return SimpleNamespace(
                fetchone=lambda: {
                    "blocker_code": self.blocker_code,
                    "detail_json": '{"transport_stale_after_s":60}',
                }
            )

    class Runtime:
        conn = Conn()
        recovered = []

        def mark_recovered(self, source_key, *, detail=None):
            self.recovered.append((source_key, detail))

    runtime = Runtime()
    monkeypatch.setattr(
        worker,
        "load_frontier",
        lambda *_args: SimpleNamespace(has_open_gap=False),
    )

    worker._mark_confirmation_recovered(runtime, ("bags_solana:ws",))
    assert runtime.recovered == []

    runtime.conn.blocker_code = "CONFIRMATION_FAILURE"
    worker._mark_confirmation_recovered(runtime, ("bags_solana:ws",))
    assert runtime.recovered == [
        ("bags_solana:ws", {"transport_stale_after_s": 60})
    ]


def test_active_solana_transport_is_pump_only():
    settings = SimpleNamespace(
        solana_rpc_url="https://pump-read.invalid/rpc",
        solana_ws_url="",
    )

    assert worker._solana_ws_transport(settings) == "wss://pump-read.invalid/rpc"


def test_indexed_solana_recovery_returns_full_transactions_and_paginates():
    calls = []
    pages = {
        None: {
            "data": [
                {"slot": 12, "blockTime": 12, "transaction": {"signatures": ["new"]}},
                {"slot": 11, "blockTime": 11, "transaction": {"signatures": ["older"]}},
            ],
            "paginationToken": "next-page",
        },
        "next-page": {
            "data": [
                {"slot": 10, "blockTime": 10, "transaction": {"signatures": ["durable"]}},
            ],
            "paginationToken": "done",
        },
    }

    def fetch_page(token):
        calls.append(token)
        return pages[token]

    result = _indexed_solana_recovery(
        fetch_page,
        previous_durable_signature="durable",
        max_pages=2,
    )

    assert calls == [None, "next-page"]
    assert [row["transaction"]["signatures"][0] for row in result.transactions] == [
        "new", "older"
    ]
    assert result.complete is True
    assert result.resume_cursor is None


def test_storage_pause_cancels_then_restarts_source_tasks():
    async def scenario():
        started = 0
        release = asyncio.Event()

        async def source_lane():
            nonlocal started
            started += 1
            await release.wait()

        def start_sources():
            return [asyncio.create_task(source_lane())]

        tracked_tasks = []
        source_tasks = start_sources()
        tracked_tasks.extend(source_tasks)
        await asyncio.sleep(0)

        paused, source_tasks = await worker._transition_ingestion_tasks(
            pause_ingestion=True,
            ingestion_paused=False,
            source_tasks=source_tasks,
            start_source_tasks=start_sources,
            tracked_tasks=tracked_tasks,
        )

        assert paused is True
        assert source_tasks == []
        assert tracked_tasks[0].cancelled()

        paused, source_tasks = await worker._transition_ingestion_tasks(
            pause_ingestion=False,
            ingestion_paused=paused,
            source_tasks=source_tasks,
            start_source_tasks=start_sources,
            tracked_tasks=tracked_tasks,
        )
        await asyncio.sleep(0)

        assert paused is False
        assert len(source_tasks) == 1
        assert started == 2
        assert tracked_tasks[-1] is source_tasks[0]

        release.set()
        await asyncio.gather(*source_tasks)

    asyncio.run(scenario())


def test_solana_confirmation_target_bounds_terminal_and_missing_states():
    now = "2026-07-26T12:02:00Z"
    recent = "2026-07-26T12:01:30Z"
    expired = "2026-07-26T11:59:00Z"

    assert _solana_confirmation_target(
        {"err": None, "confirmationStatus": "finalized"},
        received_at=recent,
        now_iso=now,
        orphan_after_s=120,
    ) is ConfirmationState.FINALIZED
    assert _solana_confirmation_target(
        {"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": None},
        received_at=recent,
        now_iso=now,
        orphan_after_s=120,
    ) is ConfirmationState.ORPHANED
    assert _solana_confirmation_target(
        None, received_at=recent, now_iso=now, orphan_after_s=120
    ) is None
    assert _solana_confirmation_target(
        None, received_at=expired, now_iso=now, orphan_after_s=120
    ) is ConfirmationState.ORPHANED


def test_tentative_event_is_tracked_before_engine_state_or_order_checks():
    pending = {}
    envelope = type("Envelope", (), {
        "confirmation": ConfirmationState.TENTATIVE,
        "transaction_id": "sig-1",
    })()
    result = type("Result", (), {
        "canonical_event_id": "event-1",
        "canonical_inserted": True,
    })()

    _track_pending_confirmation(pending, envelope, result)

    assert pending == {"sig-1": {"event-1"}}


def test_confirmation_lane_removes_orphan_and_rebuilds_once(monkeypatch):
    stop = asyncio.Event()
    pending = {"sig-1": {"event-1"}}
    reconciled = []
    failures = []
    rebuilds = []

    class Result:
        def fetchall(self):
            return []

        def fetchone(self):
            return {
                "canonical_event_id": "event-1",
                "network_id": "solana",
                "venue_id": "pumpfun_solana",
                "transaction_id": "sig-1",
                "block_or_slot": 1,
                "block_hash": None,
                "received_at": "2026-07-26T11:59:00Z",
            }

    class Conn:
        def execute(self, *_args, **_kwargs):
            return Result()

    class Runtime:
        conn = Conn()
        heartbeats = []

        def heartbeat(self, source_key, **kwargs):
            self.heartbeats.append((source_key, kwargs))

    async def rpc(*_args, **_kwargs):
        stop.set()
        return {"value": [{"err": {"InstructionError": [0, "Custom"]}}]}

    monkeypatch.setattr(worker, "_rpc_call", rpc)
    monkeypatch.setattr(worker, "real_utc_now_iso", lambda: "2026-07-26T12:02:00Z")
    monkeypatch.setattr(
        worker,
        "reconcile_confirmation",
        lambda _conn, **kwargs: reconciled.append(kwargs),
    )
    monkeypatch.setattr(
        worker,
        "record_source_failure",
        lambda _conn, **kwargs: failures.append(kwargs),
    )
    monkeypatch.setattr(worker, "_mark_confirmation_recovered", lambda *_args: None)
    resolved_orphans = []
    monkeypatch.setattr(
        worker,
        "_resolve_terminal_orphan_gaps",
        lambda *_args, **kwargs: resolved_orphans.append(kwargs),
    )

    asyncio.run(
        worker._confirmation_lane(
            Runtime.conn,
            pending,
            runtime=Runtime(),
            solana_rpc_url="https://fixture.invalid",
            robinhood_rpc_url="",
            stop=stop,
            poll_s=0.001,
            solana_orphan_after_s=120,
            rebuild_fn=lambda: rebuilds.append(True),
        )
    )

    assert reconciled[0]["confirmation"] is ConfirmationState.ORPHANED
    assert failures[0]["source_key"] == "pumpfun_solana:ws"
    assert pending == {}
    assert rebuilds == [True]
    assert resolved_orphans[0]["source_keys"] == {"pumpfun_solana:ws"}


def test_successful_rebuild_closes_the_terminal_orphan_gap(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "pumpfun_solana:ws"
    cursor = "10:durable-signature:0"
    conn.execute(
        "INSERT INTO op_raw_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "raw-durable",
            "solana",
            "pumpfun_solana",
            source_key,
            "solana_logs_subscribe",
            cursor,
            worker.PUMPFUN_PROGRAM,
            "durable-signature",
            10,
            None,
            0,
            None,
            "2026-07-26T12:00:00Z",
            "2026-07-26T12:00:00Z",
            "tentative",
            "{}",
            "payload-hash",
            "row-fingerprint",
            "2026-07-26T12:00:00Z",
        ),
    )
    record_processed_cursor(
        conn,
        source_key=source_key,
        source_cursor=cursor,
        observed_at="2026-07-26T12:00:00Z",
        decode_complete=True,
    )
    record_source_failure(
        conn,
        source_key=source_key,
        observed_at="2026-07-26T12:02:00Z",
        detail="terminal_solana_confirmation_orphan",
    )

    class Runtime:
        recovered = []

        def mark_recovered(self, key):
            self.recovered.append(key)

    runtime = Runtime()
    worker._resolve_terminal_orphan_gaps(
        conn,
        runtime,
        source_keys={source_key},
        observed_at="2026-07-26T12:02:01Z",
    )

    frontier = worker.load_frontier(conn, source_key)
    assert frontier.has_open_gap is False
    assert frontier.durable_cursor == cursor
    assert worker.list_open_gaps(conn, source_key) == []
    assert runtime.recovered == [source_key]


def test_confirmation_lane_checks_forward_entry_signature_before_fifo_backlog(monkeypatch):
    stop = asyncio.Event()
    pending = {f"sig-{index}": {f"event-{index}"} for index in range(257)}
    requested = []
    recovered = []

    class Result:
        def __init__(self, *, one=None, many=()):
            self.one = one
            self.many = many

        def fetchone(self):
            return self.one

        def fetchall(self):
            return list(self.many)

    class Conn:
        def execute(self, sql, params=()):
            if "SELECT DISTINCT event.transaction_id" in sql:
                return Result(many=({"transaction_id": "sig-256"},))
            if sql in {"BEGIN", "COMMIT", "ROLLBACK"}:
                return Result()
            event_id = params[0]
            index = int(event_id.removeprefix("event-"))
            return Result(one={
                "canonical_event_id": event_id,
                "network_id": "solana",
                "venue_id": "pumpfun_solana",
                "transaction_id": f"sig-{index}",
                "block_or_slot": index,
                "block_hash": None,
                "received_at": "2026-07-26T12:01:59Z",
            })

    class Runtime:
        conn = Conn()

        def heartbeat(self, *_args, **_kwargs):
            pass

    async def rpc(_governor, _priority, _url, _method, params):
        requested.extend(params[0])
        stop.set()
        return {"value": [None] * len(params[0])}

    monkeypatch.setattr(worker, "_rpc_call", rpc)
    monkeypatch.setattr(worker, "real_utc_now_iso", lambda: "2026-07-26T12:02:00Z")
    monkeypatch.setattr(
        worker,
        "_mark_confirmation_recovered",
        lambda _runtime, source_keys: recovered.extend(source_keys),
    )

    asyncio.run(
        worker._confirmation_lane(
            Runtime.conn,
            pending,
            runtime=Runtime(),
            solana_rpc_url="https://fixture.invalid",
            robinhood_rpc_url="",
            stop=stop,
            poll_s=0.001,
        )
    )

    assert len(requested) == 256
    assert requested[0] == "sig-256"
    assert "sig-255" not in requested
    assert recovered == ["pumpfun_solana:ws"]


def test_solana_recovery_keeps_original_gap_target_when_live_frontier_moves():
    frontier = type("Frontier", (), {"durable_cursor": "999:live-new:0", "has_open_gap": True})()
    gaps = [{
        "gap_kind": "enumeration_bound",
        "previous_durable_cursor": "100:durable-original:0",
        "resume_cursor": "older-page",
    }]

    assert _solana_recovery_cursors(frontier, gaps) == (
        "durable-original", "older-page"
    )


def test_confirmation_selection_bounds_backlog_by_signature():
    pending = {f"sig-{index}": {f"event-{index}"} for index in range(600)}

    selected = worker._selected_pending_confirmations(pending)

    assert len(selected) == 256
    assert selected[0] == ("sig-0", ("event-0",))
    assert selected[-1] == ("sig-255", ("event-255",))


def test_confirmation_selection_prioritizes_forward_entry_signatures():
    pending = {f"sig-{index}": {f"event-{index}"} for index in range(600)}

    selected = worker._selected_pending_confirmations(
        pending,
        priority_transaction_ids=("sig-599", "sig-300", "missing"),
    )

    assert len(selected) == 256
    assert selected[:3] == (
        ("sig-599", ("event-599",)),
        ("sig-300", ("event-300",)),
        ("sig-0", ("event-0",)),
    )
    assert len({signature for signature, _events in selected}) == 256


def test_recovery_batch_chunks_stay_under_provider_response_limit():
    assert worker._bounded_chunks(list(range(8))) == (
        (0, 1, 2), (3, 4, 5), (6, 7)
    )


def test_recovery_falls_back_to_paced_single_calls_when_provider_forbids_batches(monkeypatch):
    calls = []

    def batch(_url, _requests, **_kwargs):
        calls.append("batch")
        raise HTTPError("https://fixture.invalid", 403, "Forbidden", None, None)

    def single(_url, method, params, **_kwargs):
        calls.append((method, params[0]))
        return {"signature": params[0]}

    class Governor:
        async def call(self, _priority, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    monkeypatch.setattr(worker, "json_rpc_batch_http", batch)
    monkeypatch.setattr(worker, "json_rpc_http", single)

    result = asyncio.run(worker._fetch_solana_transactions(
        "https://fixture.invalid", ["sig-1", "sig-2", "sig-3", "sig-4"],
        rpc_governor=Governor(),
    ))

    assert result == [
        {"signature": "sig-1"}, {"signature": "sig-2"},
        {"signature": "sig-3"}, {"signature": "sig-4"},
    ]
    assert calls == [
        "batch",
        ("getTransaction", "sig-1"),
        ("getTransaction", "sig-2"),
        ("getTransaction", "sig-3"),
        ("getTransaction", "sig-4"),
    ]


def test_evm_receipt_requires_canonical_block_identity_and_success():
    row = {"block_hash": "0xabc"}
    assert _evm_receipt_confirmation(
        row, {"blockHash": "0xAbC", "status": "0x1"}
    ) is ConfirmationState.FINALIZED
    assert _evm_receipt_confirmation(row, None) is ConfirmationState.ORPHANED
    assert _evm_receipt_confirmation(
        row, {"blockHash": "0xdef", "status": "0x1"}
    ) is ConfirmationState.ORPHANED
    assert _evm_receipt_confirmation(
        row, {"blockHash": "0xabc", "status": "0x0"}
    ) is ConfirmationState.ORPHANED


def test_solana_order_comes_from_native_block_signature_position():
    block = {
        "blockhash": "block-1",
        "blockTime": 1_700_000_000,
        "signatures": ["sig-a", "sig-b", "sig-c"],
    }
    assert _solana_order_from_block(block, "sig-b") == (1, "block-1", 1_700_000_000)


def test_solana_event_context_does_not_block_fast_path_on_read_rpc(monkeypatch):
    methods = []

    def rpc(_url, method, _params):
        methods.append(method)
        raise AssertionError(f"unexpected Pump read RPC: {method}")

    monkeypatch.setattr(worker, "json_rpc_http", rpc)
    cache = {}

    first = asyncio.run(
        _solana_event_context(
            venue=worker.VenueId.PUMPFUN_SOLANA,
            rpc_url="https://fixture.invalid",
            signature="sig-a",
            slot=100,
            block_cache=cache,
        )
    )
    second = asyncio.run(
        _solana_event_context(
            venue=worker.VenueId.PUMPFUN_SOLANA,
            rpc_url="https://fixture.invalid",
            signature="sig-b",
            slot=100,
            block_cache=cache,
        )
    )
    bags = asyncio.run(
        _solana_event_context(
            venue=worker.VenueId.BAGS_SOLANA,
            rpc_url="https://fixture.invalid",
            signature="sig-c",
            slot=101,
            block_cache=cache,
        )
    )

    assert first == (None, None, None, None)
    assert second == (None, None, None, None)
    assert bags == (None, None, None, None)
    assert methods == []


def test_evm_finality_uses_finalized_block_tag_result():
    assert _finalized_evm_height({"number": "0x20"}) == 32
    assert _finalized_evm_height(None) is None
    assert _finalized_evm_height({"number": None}) is None


def test_solana_confirmation_batch_is_unique_and_bounded():
    rows = [{"transaction_id": f"sig-{index // 2}"} for index in range(300)]
    signatures = _unique_confirmation_signatures(rows, limit=128)
    assert len(signatures) == 128
    assert signatures[:2] == ["sig-0", "sig-1"]
    assert signatures[-1] == "sig-127"


def test_solana_recovery_runs_only_after_subscription_is_active(monkeypatch):
    stop = asyncio.Event()
    order = []

    class WebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            order.append("subscribe_sent")

        async def recv(self):
            order.append("subscription_ack")
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})

    async def recover(*_args, **_kwargs):
        order.append("recovery")
        stop.set()

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_recover_solana_frontier", recover)

    asyncio.run(worker._solana_lane(
        Runtime(), venue=worker.VenueId.PUMPFUN_SOLANA,
        program_id=worker.PUMPFUN_PROGRAM, ws_url="wss://fixture.invalid",
        rpc_url="https://fixture.invalid", recovery_max_pages=1, stop=stop,
    ))

    assert order == ["subscribe_sent", "subscription_ack", "recovery"]


def test_solana_multi_lane_starts_one_bounded_recovery_for_bags_shard(monkeypatch):
    stop = asyncio.Event()
    sent = []
    recovered = []
    connect_options = {}
    abandon = object()

    class WebSocket:
        replies = iter(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "result": 8}),
            ]
        )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, frame):
            sent.append(json.loads(frame))

        async def recv(self):
            reply = next(self.replies)
            if '"id": 2' in reply:
                stop.set()
            return reply

    recovery_urls = []

    async def recover(*_args, venue, rpc_url, abandon_fn=None, **_kwargs):
        recovered.append((venue, abandon_fn))
        recovery_urls.append(rpc_url)
        await stop.wait()

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    def connect(*_args, **kwargs):
        connect_options.update(kwargs)
        return WebSocket()

    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(worker, "_solana_recovery_loop", recover)

    asyncio.run(
        _solana_multi_lane(
            Runtime(),
            ws_url="wss://fixture.invalid",
            rpc_url="https://fixture.invalid",
            recovery_rpc_url="https://api.mainnet-beta.solana.com",
            recovery_max_pages=1,
            stop=stop,
            block_cache={},
            bags_pool_mints={"Pool1": "Mint1", "Pool2": "Mint2"},
            include_pump=False,
            abandon_fn=abandon,
        )
    )

    assert [frame["id"] for frame in sent] == [1, 2]
    assert [frame["params"][0]["mentions"][0] for frame in sent] == [
        "Pool1",
        "Pool2",
    ]
    assert recovered == [(worker.VenueId.BAGS_SOLANA, abandon)]
    assert recovery_urls == ["https://api.mainnet-beta.solana.com"]
    assert connect_options["ping_interval"] == 20
    assert connect_options["ping_timeout"] == 60
    assert connect_options["max_queue"] == 4096


def test_solana_multi_lane_restarts_bounded_recovery_for_a_new_same_socket_gap(monkeypatch):
    stop = asyncio.Event()
    recovered = []

    class WebSocket:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})
            if len(recovered) >= 2 or self.calls >= 4:
                stop.set()
            await asyncio.sleep(0)
            raise asyncio.TimeoutError

    async def recover(*_args, **_kwargs):
        recovered.append("recovery")

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return len(recovered) == 1

        def heartbeat(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_solana_recovery_loop", recover)

    asyncio.run(
        _solana_multi_lane(
            Runtime(),
            ws_url="wss://fixture.invalid",
            rpc_url="https://fixture.invalid",
            recovery_max_pages=1,
            stop=stop,
            block_cache={},
            include_pump=True,
        )
    )

    assert recovered == ["recovery", "recovery"]


def test_only_primary_bags_shard_owns_the_shared_source_recovery(monkeypatch):
    stop = asyncio.Event()
    recovered = []
    pool = next(
        f"Pool{index}"
        for index in range(100)
        if worker._bags_pool_shard(f"Pool{index}", 4) == 1
    )

    class WebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            stop.set()
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})

    async def recover(*_args, **_kwargs):
        recovered.append("recovery")

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_solana_recovery_loop", recover)

    asyncio.run(
        _solana_multi_lane(
            Runtime(),
            ws_url="wss://fixture.invalid",
            rpc_url="https://fixture.invalid",
            recovery_max_pages=1,
            stop=stop,
            block_cache={},
            bags_pool_mints={pool: "Mint1"},
            include_pump=False,
            bags_shard_index=1,
            bags_shard_count=4,
        )
    )

    assert recovered == []


def test_bags_pool_shards_are_stable_disjoint_and_bounded():
    pools = [f"Pool{index}" for index in range(100)]
    shards = [
        {pool for pool in pools if worker._bags_pool_shard(pool, 4) == shard}
        for shard in range(4)
    ]

    assert set().union(*shards) == set(pools)
    assert sum(len(shard) for shard in shards) == len(pools)
    assert max(map(len, shards)) < 50
    assert worker._bags_pool_shard("Pool42", 4) == worker._bags_pool_shard("Pool42", 4)


def test_solana_multi_lane_keeps_receiving_while_recovery_is_running(monkeypatch):
    stop = asyncio.Event()
    recovery_release = asyncio.Event()
    recovery_started = asyncio.Event()
    submitted = []

    notification = {
        "params": {
            "subscription": 7,
            "result": {
                "context": {"slot": 123},
                "value": {
                    "signature": "sig-live",
                    "err": None,
                    "logs": [f"Program {worker.PUMPFUN_PROGRAM} success"],
                },
            },
        }
    }

    class WebSocket:
        replies = iter(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7}),
                json.dumps(notification),
            ]
        )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            return next(self.replies)

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    async def recover(*_args, **_kwargs):
        recovery_started.set()
        await recovery_release.wait()

    async def submit_live(*_args, parsed, **_kwargs):
        submitted.append(parsed["signature"])
        stop.set()

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "record_source_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "_recover_solana_frontier", recover)
    monkeypatch.setattr(worker, "_submit_solana_parsed", submit_live)

    async def run():
        try:
            await asyncio.wait_for(
                _solana_multi_lane(
                    Runtime(),
                    ws_url="wss://fixture.invalid",
                    rpc_url="https://fixture.invalid",
                    recovery_max_pages=1,
                    stop=stop,
                    block_cache={},
                    bags_pool_mints={},
                    include_pump=True,
                ),
                timeout=0.25,
            )
        finally:
            recovery_release.set()

    asyncio.run(run())

    assert recovery_started.is_set()
    assert submitted == ["sig-live"]


def test_solana_recovery_loop_checkpoints_one_page_at_a_time(monkeypatch):
    stop = asyncio.Event()
    page_limits = []
    results = iter((False, False, True))

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return True

    async def recover(*_args, max_pages, **_kwargs):
        page_limits.append(max_pages)
        return next(results)

    monkeypatch.setattr(worker, "_recover_solana_frontier", recover)
    asyncio.run(
        worker._solana_recovery_loop(
            Runtime(),
            venue=worker.VenueId.PUMPFUN_SOLANA,
            program_id=worker.PUMPFUN_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=20,
            stop=stop,
        )
    )

    assert page_limits == [1, 1, 1]


def test_pump_restart_recovery_freezes_frontier_before_live_events(monkeypatch, tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "pumpfun_solana:ws"
    cursor = "100:sig-before-restart:0"
    conn.execute(
        "INSERT INTO op_raw_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "raw-before-restart", "solana", "pumpfun_solana", source_key,
            "solana_logs_subscribe", cursor, "mint", "sig-before-restart",
            100, None, 0, None, "2026-07-26T12:00:00Z",
            "2026-07-26T12:00:00Z", "tentative", "{}", "payload-hash",
            "row-fingerprint", "2026-07-26T12:00:00Z",
        ),
    )
    record_processed_cursor(
        conn,
        source_key=source_key,
        source_cursor=cursor,
        observed_at="2026-07-26T12:00:00Z",
        decode_complete=True,
    )

    class Runtime:
        def __init__(self):
            self.conn = conn
            self.gapped = False

        def source_gapped(self, _source_key):
            return self.gapped

        def heartbeat(self, _source_key, *, blocker_code=None, **_kwargs):
            self.gapped = blocker_code is not None

    runtime = Runtime()
    observed = []

    async def recover(*_args, **_kwargs):
        frontier = worker.load_frontier(conn, source_key)
        observed.append((runtime.source_gapped(source_key), frontier))
        return True

    monkeypatch.setattr(worker, "_recover_solana_frontier", recover)
    monkeypatch.setattr(
        worker, "real_utc_now_iso", lambda: "2026-07-26T12:00:01Z"
    )
    asyncio.run(
        worker._solana_recovery_loop(
            runtime,
            venue=worker.VenueId.PUMPFUN_SOLANA,
            program_id=worker.PUMPFUN_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
            stop=asyncio.Event(),
        )
    )

    assert observed[0][0] is True
    assert observed[0][1].has_open_gap is True
    assert observed[0][1].durable_cursor == cursor


def test_solana_recovery_loop_rebases_after_bounded_page_budget(monkeypatch):
    stop = asyncio.Event()
    calls = 0
    rebased = []

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return True

    async def recover(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return False

    def rebase(venue, observed_at):
        rebased.append((venue, observed_at))

    monkeypatch.setattr(worker, "_recover_solana_frontier", recover)
    asyncio.run(
        worker._solana_recovery_loop(
            Runtime(),
            venue=worker.VenueId.PUMPFUN_SOLANA,
            program_id=worker.PUMPFUN_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=2,
            stop=stop,
            abandon_fn=rebase,
        )
    )

    assert calls == 2
    assert [item[0] for item in rebased] == [worker.VenueId.PUMPFUN_SOLANA]


def test_bags_recovery_loop_rebases_without_global_program_enumeration(monkeypatch):
    stop = asyncio.Event()
    retried = []
    rebased = []

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return True

    def retry(*_args, **_kwargs):
        retried.append(True)
        return 1

    async def recover(*_args, **_kwargs):
        raise AssertionError("Bags recovery must not enumerate the global DBC program")

    monkeypatch.setattr(worker, "_retry_stored_solana_decode_gaps", retry)
    monkeypatch.setattr(worker, "frontier_gap_is_discardable", lambda *_args: True)
    monkeypatch.setattr(worker, "_recover_solana_frontier", recover)

    asyncio.run(
        worker._solana_recovery_loop(
            Runtime(),
            venue=worker.VenueId.BAGS_SOLANA,
            program_id=worker.BAGS_DBC_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
            stop=stop,
            abandon_fn=lambda venue, observed_at: rebased.append(
                (venue, observed_at)
            ),
            bags_pool_mints={"pool-1": "mint-1"},
        )
    )

    assert retried == [True]
    assert [item[0] for item in rebased] == [worker.VenueId.BAGS_SOLANA]


def test_quiet_pump_socket_uses_ping_heartbeat_without_opening_fake_gap(monkeypatch):
    stop = asyncio.Event()
    connect_options = {}

    class WebSocket:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})
            stop.set()
            raise asyncio.TimeoutError

    class Runtime:
        conn = object()

        def __init__(self):
            self.heartbeats = []

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, source_key, *_args, **_kwargs):
            self.heartbeats.append(source_key)

    def connect(*_args, **kwargs):
        connect_options.update(kwargs)
        return WebSocket()

    failures = []
    runtime = Runtime()
    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(
        worker, "_solana_recovery_loop", lambda *_args, **_kwargs: asyncio.sleep(0)
    )
    monkeypatch.setattr(worker, "record_source_failure", lambda *_args, **_kwargs: failures.append(1))
    asyncio.run(
        _solana_multi_lane(
            runtime,
            ws_url="wss://fixture.invalid",
            rpc_url="https://fixture.invalid",
            recovery_max_pages=1,
            stop=stop,
            block_cache={},
            bags_pool_mints={},
            include_pump=True,
        )
    )

    assert failures == []
    assert runtime.heartbeats[-1] == "pumpfun_solana:ws"
    assert connect_options["ping_interval"] == 20


def test_solana_transport_health_separates_socket_and_bags_event_freshness():
    class Runtime:
        def __init__(self):
            self.kwargs = None
            self.conn = None

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, _source_key, **kwargs):
            self.kwargs = kwargs

    runtime = Runtime()

    # pump.fun DOES declare an expectation. It used to be grouped with quiet Bags, and
    # this test asserted the absence of the bound -- which is what let a dead
    # subscription read as healthy for 20 minutes on 2026-08-07. `logsSubscribe` was
    # accepted by `solana-rpc.publicnode.com`, returned an id, and delivered nothing.
    worker._source_heartbeat(runtime, "pumpfun_solana:ws")

    assert runtime.kwargs["detail"]["transport_stale_after_s"] == 60
    assert runtime.kwargs["detail"]["expected_event_max_age_s"] == 300
    assert runtime.kwargs["detail"]["event_expectation_started_at"]

    # Bags is a quiet venue: its newest pools trade hours apart, so it declares transport
    # liveness only. A 900s event bound there reported a working socket as a dead one.
    worker._source_heartbeat(runtime, "bags_solana:ws")

    assert runtime.kwargs["detail"] == {"transport_stale_after_s": 60}

    # The separation still has to be demonstrable, so assert it on the high-cadence source
    # that legitimately declares an expectation. Otherwise this test would only prove that
    # event freshness is never checked anywhere.
    worker._source_heartbeat(runtime, "pons_v1_4663:ws")

    assert runtime.kwargs["detail"]["transport_stale_after_s"] > 0
    assert runtime.kwargs["detail"]["expected_event_max_age_s"] == 300


def test_combined_rpc_metrics_include_recovery_pressure_and_throttle():
    combined = worker._combined_rpc_metrics(
        {
            "current_rps": 0.25,
            "configured_concurrency": 1,
            "active_calls": 0,
            "max_observed_concurrency": 1,
            "queue_depth": 2,
            "rate_limit_events": 0,
            "throttled": False,
        },
        {
            "current_rps": 2.0,
            "configured_concurrency": 1,
            "active_calls": 1,
            "max_observed_concurrency": 1,
            "queue_depth": 3,
            "rate_limit_events": 1,
            "throttled": True,
        },
        {
            "current_rps": 0.25,
            "configured_concurrency": 1,
            "active_calls": 0,
            "max_observed_concurrency": 1,
            "queue_depth": 4,
            "rate_limit_events": 0,
            "throttled": False,
        },
    )

    assert combined["current_rps"] == 2.5
    assert combined["configured_concurrency"] == 3
    assert combined["active_calls"] == 1
    assert combined["queue_depth"] == 9
    assert combined["rate_limit_events"] == 1
    assert combined["throttled"] is True
    assert combined["recovery"]["current_rps"] == 2.0
    assert combined["robinhood"]["current_rps"] == 0.25


@pytest.mark.parametrize("batch", [False, True], ids=("single", "batch"))
def test_governed_rpc_owns_the_only_rate_limit_retry_layer(monkeypatch, batch):
    attempts = 0

    def rate_limited(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise HTTPError(
            "https://fixture.invalid", 429, "limited", {"Retry-After": "0"}, None
        )

    async def exercise():
        governor = worker.OpportunityRpcGovernor(
            concurrency=1,
            initial_rps=1_000,
            min_rps=1_000,
            max_rps=1_000,
        )
        try:
            with pytest.raises(HTTPError):
                if batch:
                    await worker._rpc_batch_call(
                        governor,
                        worker.RpcPriority.CURRENT,
                        "https://fixture.invalid",
                        [("eth_getLogs", [{}])],
                    )
                else:
                    await worker._rpc_call(
                        governor,
                        worker.RpcPriority.CURRENT,
                        "https://fixture.invalid",
                        "eth_blockNumber",
                        [],
                    )
            return governor.metrics()
        finally:
            await governor.close()

    monkeypatch.setattr("urllib.request.urlopen", rate_limited)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    metrics = asyncio.run(exercise())

    assert attempts == 4
    assert metrics["rate_limit_events"] == 3


def test_solana_multi_lane_heartbeats_the_source_owned_by_each_socket(monkeypatch):
    stop = asyncio.Event()

    class WebSocket:
        replies = iter(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7}),
                json.dumps({"jsonrpc": "2.0", "method": "unrelatedTraffic"}),
            ]
        )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            reply = next(self.replies)
            if "unrelatedTraffic" in reply:
                stop.set()
            return reply

    class Runtime:
        conn = object()

        def __init__(self):
            self.heartbeats = []

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, source_key, *_args, **_kwargs):
            self.heartbeats.append(source_key)

    async def recover(*_args, **_kwargs):
        pass

    runtime = Runtime()
    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_recover_solana_frontier", recover)

    asyncio.run(
        _solana_multi_lane(
            runtime,
            ws_url="wss://fixture.invalid",
            rpc_url="https://fixture.invalid",
            recovery_max_pages=1,
            stop=stop,
            block_cache={},
            bags_pool_mints={"Pool1": "Mint1"},
            include_pump=False,
        )
    )

    assert runtime.heartbeats.count("pumpfun_solana:ws") == 0
    assert runtime.heartbeats.count("bags_solana:ws") == 1


def test_quiet_bags_pool_socket_stays_healthy_without_fake_message_volume(monkeypatch):
    stop = asyncio.Event()

    class WebSocket:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})
            stop.set()
            raise asyncio.TimeoutError

    class Runtime:
        conn = object()

        def __init__(self):
            self.heartbeats = []

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, source_key, *_args, **_kwargs):
            self.heartbeats.append(source_key)

    failures = []
    runtime = Runtime()
    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "record_source_failure", lambda *_args, **_kwargs: failures.append(1))
    asyncio.run(
        _solana_multi_lane(
            runtime,
            ws_url="wss://fixture.invalid",
            rpc_url="https://fixture.invalid",
            recovery_max_pages=1,
            stop=stop,
            block_cache={},
            bags_pool_mints={"Pool1": "Mint1"},
            include_pump=False,
        )
    )

    assert failures == []
    assert runtime.heartbeats[-1] == "bags_solana:ws"


def test_bags_pool_replacement_waits_for_unsubscribe_ack(monkeypatch):
    stop = asyncio.Event()
    pool_mints = {"Pool1": "Mint1"}
    sent = []
    unsubscribe_acked = False

    class WebSocket:
        replies = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, frame):
            message = json.loads(frame)
            if (
                message["method"] == "logsSubscribe"
                and message["params"][0]["mentions"][0] == "Pool1"
            ):
                pool_mints.clear()
                pool_mints["Pool2"] = "Mint2"
            if (
                message["method"] == "logsSubscribe"
                and message["params"][0]["mentions"][0] == "Pool2"
            ):
                assert unsubscribe_acked is True
            sent.append(message)

        async def recv(self):
            nonlocal unsubscribe_acked
            self.replies += 1
            if self.replies == 1:
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})
            if self.replies == 2:
                unsubscribe_acked = True
                return json.dumps({"jsonrpc": "2.0", "id": 2, "result": True})
            stop.set()
            return json.dumps({"jsonrpc": "2.0", "id": 3, "result": 8})

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_BAGS_WS_SUBSCRIPTION_CAP", 1)

    asyncio.run(
        _solana_multi_lane(
            Runtime(),
            ws_url="wss://fixture.invalid",
            rpc_url="https://fixture.invalid",
            recovery_max_pages=1,
            stop=stop,
            block_cache={},
            bags_pool_mints=pool_mints,
            include_pump=False,
        )
    )

    assert [message["method"] for message in sent] == [
        "logsSubscribe", "logsUnsubscribe", "logsSubscribe",
    ]


def test_solana_recovery_skips_known_pump_auxiliary_events(monkeypatch, tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    conn.execute(
        "INSERT INTO op_raw_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "raw-durable",
            "solana",
            "pumpfun_solana",
            "pumpfun_solana:ws",
            "solana_logs_subscribe",
            "9:durable:0",
            worker.PUMPFUN_PROGRAM,
            "durable",
            9,
            None,
            0,
            None,
            "2026-07-26T11:59:59Z",
            "2026-07-26T11:59:59Z",
            "tentative",
            "{}",
            "payload-durable",
            "fingerprint-durable-auxiliary",
            "2026-07-26T11:59:59Z",
        ),
    )
    record_processed_cursor(
        conn,
        source_key="pumpfun_solana:ws",
        source_cursor="9:durable:0",
        observed_at="2026-07-26T11:59:59Z",
        decode_complete=True,
    )
    record_source_failure(
        conn,
        source_key="pumpfun_solana:ws",
        observed_at="2026-07-26T12:00:00Z",
        detail="fixture disconnect",
    )
    discriminator = bytes.fromhex("929fbdac925838f4")
    auxiliary_log = "Program data: " + base64.b64encode(discriminator).decode()

    methods = []

    def rpc(_url, method, _params):
        methods.append(method)
        if method == "getSignaturesForAddress":
            return [{"signature": "sig-1"}, {"signature": "durable"}]
        if method == "getTransaction":
            return {
                "slot": 10,
                "meta": {
                    "logMessages": [
                        f"Program {worker.PUMPFUN_PROGRAM} invoke [1]",
                        auxiliary_log,
                        f"Program {worker.PUMPFUN_PROGRAM} success",
                    ]
                },
                "transaction": {"message": {"accountKeys": ["actor-1"]}},
            }
        raise AssertionError(method)

    class Runtime:
        def __init__(self):
            self.submitted = []

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, *, max_items):
            assert max_items == 1

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

        def mark_recovered(self, *_args, **_kwargs):
            pass

    runtime = Runtime()
    monkeypatch.setattr(worker, "json_rpc_http", rpc)

    asyncio.run(
        worker._recover_solana_frontier(
            conn,
            runtime,
            venue=worker.VenueId.PUMPFUN_SOLANA,
            program_id=worker.PUMPFUN_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
        )
    )

    assert runtime.submitted == []
    assert methods == ["getSignaturesForAddress", "getTransaction"]


def test_indexed_solana_recovery_uses_full_history_without_per_signature_reads(
    monkeypatch, tmp_path
):
    conn = connect(tmp_path / "opportunity.sqlite")
    record_source_failure(
        conn,
        source_key="pumpfun_solana:ws",
        observed_at="2026-07-26T12:00:00Z",
        detail="fixture disconnect",
    )
    discriminator = bytes.fromhex("929fbdac925838f4")
    auxiliary_log = "Program data: " + base64.b64encode(discriminator).decode()
    methods = []

    def rpc(_url, method, _params):
        methods.append(method)
        assert method == "getTransactionsForAddress"
        return {
            "data": [
                {
                    "slot": 10,
                    "blockTime": 2_000_000_000,
                    "meta": {"logMessages": [auxiliary_log]},
                    "transaction": {
                        "signatures": ["sig-1"],
                        "message": {"accountKeys": ["actor-1"]},
                    },
                },
                {
                    "slot": 9,
                    "blockTime": 1,
                    "meta": {"logMessages": []},
                    "transaction": {
                        "signatures": ["before-gap"],
                        "message": {"accountKeys": ["actor-2"]},
                    },
                },
            ],
            "paginationToken": "next-page",
        }

    class Runtime:
        submitted = []

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, *, max_items):
            assert max_items == 1

        def heartbeat(self, *_args, **_kwargs):
            pass

        def mark_recovered(self, *_args, **_kwargs):
            pass

    runtime = Runtime()
    monkeypatch.setattr(worker, "json_rpc_http", rpc)

    complete = asyncio.run(
        worker._recover_solana_frontier(
            conn,
            runtime,
            venue=worker.VenueId.PUMPFUN_SOLANA,
            program_id=worker.PUMPFUN_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
            indexed_recovery=True,
        )
    )

    assert complete is True
    assert runtime.submitted == []
    assert methods == ["getTransactionsForAddress"]


def test_indexed_solana_recovery_skips_a_cursor_already_in_the_raw_ledger(
    monkeypatch, tmp_path
):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "pumpfun_solana:ws"
    for raw_id, cursor, signature, slot in (
        ("raw-durable", "9:durable:0", "durable", 9),
        ("raw-duplicate", "10:duplicate:0", "duplicate", 10),
    ):
        conn.execute(
            "INSERT INTO op_raw_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                raw_id,
                "solana",
                "pumpfun_solana",
                source_key,
                "solana_logs_subscribe",
                cursor,
                worker.PUMPFUN_PROGRAM,
                signature,
                slot,
                None,
                0,
                None,
                "2026-07-26T12:00:00Z",
                "2026-07-26T12:00:00Z",
                "tentative",
                "{}",
                f"payload-{raw_id}",
                f"fingerprint-{raw_id}",
                "2026-07-26T12:00:00Z",
            ),
        )
    record_processed_cursor(
        conn,
        source_key=source_key,
        source_cursor="9:durable:0",
        observed_at="2026-07-26T12:00:00Z",
        decode_complete=True,
    )
    record_source_failure(
        conn,
        source_key=source_key,
        observed_at="2026-07-26T12:00:01Z",
        detail="fixture disconnect",
    )

    def rpc(_url, method, _params):
        assert method == "getTransactionsForAddress"
        return {
            "data": [
                {
                    "slot": slot,
                    "blockTime": 2_000_000_000,
                    "meta": {"logMessages": ["fixture-event"]},
                    "transaction": {
                        "signatures": [signature],
                        "message": {"accountKeys": ["actor"]},
                    },
                }
                for slot, signature in (
                    (11, "new"),
                    (10, "duplicate"),
                    (9, "durable"),
                )
            ],
            "paginationToken": "next-page",
        }

    class Runtime:
        submitted = []

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, *, max_items):
            assert max_items == 1

        def heartbeat(self, *_args, **_kwargs):
            pass

        def mark_recovered(self, *_args, **_kwargs):
            pass

    runtime = Runtime()
    monkeypatch.setattr(worker, "json_rpc_http", rpc)
    monkeypatch.setattr(
        worker, "program_data_logs", lambda _logs, _program_id: [(0, "fixture-event")]
    )

    assert asyncio.run(
        worker._recover_solana_frontier(
            conn,
            runtime,
            venue=worker.VenueId.PUMPFUN_SOLANA,
            program_id=worker.PUMPFUN_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
            indexed_recovery=True,
        )
    ) is True
    assert [row.transaction_id for row in runtime.submitted] == ["new"]


def test_solana_recovery_retries_a_stored_decode_gap_with_its_feed_identity(
    monkeypatch, tmp_path
):
    conn = connect(tmp_path / "opportunity.sqlite")
    pool_raw, config_raw = bytes([10]) * 32, bytes([11]) * 32
    pool = base58_encode(pool_raw)
    mint = base58_encode(bytes([13]) * 32)
    swap = b"".join(
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
    envelope = worker.OpportunitySourceEnvelope(
        existing_raw_event_id="historical-raw-event-id",
        source_key="bags_solana:ws",
        source_provider="solana_logs_subscribe",
        source_cursor="100:sig-feed:0",
        venue_id=worker.VenueId.BAGS_SOLANA,
        program_or_contract_id=worker.BAGS_DBC_PROGRAM,
        transaction_id="sig-feed",
        block_or_slot=100,
        transaction_index=None,
        event_index=0,
        block_hash=None,
        chain_timestamp="2026-07-26T12:00:00Z",
        received_at="2026-07-26T12:00:00.100000Z",
        confirmation=ConfirmationState.TENTATIVE,
        payload={
            "log_line": "Program data: "
            + base64.b64encode(
                bytes([27, 60, 21, 213, 138, 170, 187, 147]) + swap
            ).decode(),
            "actor_id": "actor-1",
            "pool_id": pool,
        },
        token_hint=mint,
    )
    with monkeypatch.context() as seed_patch:
        seed_patch.setattr(
            opportunity_sources,
            "_decode",
            lambda *_args: blocked(
                DecodeBlocker.MISSING_IDENTITY, "historical-decoder"
            ),
        )
        seeded = worker.process_envelope(
            conn, envelope, identities=worker.VenueIdentityMap()
        )
    assert seeded.blocker == "MISSING_TOKEN_OR_CREATOR_IDENTITY"
    record_processed_cursor(
        conn,
        source_key=envelope.source_key,
        source_cursor=envelope.source_cursor,
        observed_at=envelope.received_at,
        decode_complete=False,
        detail="MISSING_TOKEN_OR_CREATOR_IDENTITY",
    )

    identities = worker.VenueIdentityMap()

    def process(stored_envelope):
        result = worker.process_envelope(
            conn, stored_envelope, identities=identities
        )
        record_processed_cursor(
            conn,
            source_key=stored_envelope.source_key,
            source_cursor=stored_envelope.source_cursor,
            observed_at=stored_envelope.received_at,
            decode_complete=result.blocker is None,
            detail=result.blocker,
        )

    runtime = worker.OpportunityCaptureRuntime(
        conn,
        settings=type("Settings", (), {"queue_max": 8})(),
        process_fn=process,
        now_fn=lambda: "2026-07-26T12:00:01Z",
    )

    def rpc(_url, method, _params):
        assert method == "getSignaturesForAddress"
        return [{"signature": "sig-feed"}]

    monkeypatch.setattr(worker, "json_rpc_http", rpc)
    complete = asyncio.run(
        worker._recover_solana_frontier(
            conn,
            runtime,
            venue=worker.VenueId.BAGS_SOLANA,
            program_id=worker.BAGS_DBC_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
        )
    )

    assert complete is True
    assert worker.list_open_gaps(conn, envelope.source_key) == []
    assert runtime.source_gapped(envelope.source_key) is False
    assert conn.execute("SELECT count(*) FROM op_raw_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM op_canonical_events").fetchone()[0] == 1


def test_bags_recovery_ignores_swaps_outside_the_feed_pool_map(
    monkeypatch, tmp_path
):
    conn = connect(tmp_path / "opportunity.sqlite")
    record_source_failure(
        conn,
        source_key="bags_solana:ws",
        observed_at="2026-07-26T12:00:00Z",
        detail="fixture disconnect",
    )
    unknown_pool, config = bytes([21]) * 32, bytes([22]) * 32
    known_pool_raw = bytes([10]) * 32
    known_pool = base58_encode(known_pool_raw)
    known_mint = base58_encode(bytes([13]) * 32)
    swap = b"".join(
        (
            unknown_pool,
            config,
            b"\x01\x00",
            struct.pack("<QQ", 25_000, 90_000),
            struct.pack("<QQ", 25_000, 100_000),
            (123456789).to_bytes(16, "little"),
            struct.pack("<QQQ", 200, 20, 0),
            struct.pack("<QQ", 25_000, 1_785_067_201),
        )
    )
    log_line = "Program data: " + base64.b64encode(
        bytes([27, 60, 21, 213, 138, 170, 187, 147]) + swap
    ).decode()
    known_event = (
        bytes([27, 60, 21, 213, 138, 170, 187, 147])
        + known_pool_raw
        + swap[32:]
    )

    def rpc(_url, method, params):
        if method == "getSignaturesForAddress":
            return [
                {"signature": "sig-unknown", "blockTime": 1_785_067_201},
                {"signature": "before-gap", "blockTime": 1_785_067_199},
            ]
        if method == "getTransaction":
            assert params[0] == "sig-unknown"
            return {
                "slot": 123,
                "blockTime": 1_785_067_201,
                "meta": {
                    "logMessages": [],
                    "loadedAddresses": {"writable": [], "readonly": []},
                    "innerInstructions": [{
                        "index": 2,
                        "instructions": [{
                            "programId": worker.BAGS_DBC_PROGRAM,
                            "data": base58_encode(
                                bytes.fromhex("e445a52e51cb9a1d")
                                + base64.b64decode(log_line.split()[-1])
                            ),
                        }, {
                            "programId": worker.BAGS_DBC_PROGRAM,
                            "data": base58_encode(
                                bytes.fromhex("e445a52e51cb9a1d") + known_event
                            ),
                        }],
                    }],
                },
                "transaction": {"message": {"accountKeys": ["actor-1"]}},
            }
        raise AssertionError(method)

    class Runtime:
        def __init__(self):
            self.submitted = []

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, **_kwargs):
            pass

        def heartbeat(self, *_args, **_kwargs):
            pass

        def mark_recovered(self, *_args, **_kwargs):
            pass

    runtime = Runtime()
    monkeypatch.setattr(worker, "json_rpc_http", rpc)

    complete = asyncio.run(
        worker._recover_solana_frontier(
            conn,
            runtime,
            venue=worker.VenueId.BAGS_SOLANA,
            program_id=worker.BAGS_DBC_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
            bags_pool_mints={known_pool: known_mint},
        )
    )

    assert complete is True
    assert len(runtime.submitted) == 1
    assert runtime.submitted[0].token_hint == known_mint
    assert runtime.submitted[0].payload["pool_id"] == known_pool


def test_stored_unknown_bags_recovery_gap_becomes_out_of_scope(
    tmp_path,
):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "bags_solana:ws"
    record_source_failure(
        conn,
        source_key=source_key,
        observed_at="2026-07-26T12:00:00Z",
        detail="fixture disconnect",
    )
    unknown_pool, config = bytes([21]) * 32, bytes([22]) * 32
    swap = b"".join(
        (
            unknown_pool,
            config,
            b"\x01\x00",
            struct.pack("<QQ", 25_000, 90_000),
            struct.pack("<QQ", 25_000, 100_000),
            (123456789).to_bytes(16, "little"),
            struct.pack("<QQQ", 200, 20, 0),
            struct.pack("<QQ", 25_000, 1_785_067_201),
        )
    )
    envelope = worker.OpportunitySourceEnvelope(
        source_key=source_key,
        source_provider="solana_rpc_recovery",
        source_cursor="123:sig-unknown:0",
        venue_id=worker.VenueId.BAGS_SOLANA,
        program_or_contract_id=worker.BAGS_DBC_PROGRAM,
        transaction_id="sig-unknown",
        block_or_slot=123,
        transaction_index=None,
        event_index=0,
        block_hash=None,
        chain_timestamp="2026-07-26T12:00:01Z",
        received_at="2026-07-26T12:00:01.100000Z",
        confirmation=ConfirmationState.TENTATIVE,
        payload={
            "log_line": "Program data: "
            + base64.b64encode(
                bytes([27, 60, 21, 213, 138, 170, 187, 147]) + swap
            ).decode(),
            "actor_id": "actor-1",
        },
        token_hint=worker.BAGS_DBC_PROGRAM,
    )
    result = worker.process_envelope(
        conn, envelope, identities=worker.VenueIdentityMap()
    )
    assert result.blocker == "MISSING_TOKEN_OR_CREATOR_IDENTITY"
    record_processed_cursor(
        conn,
        source_key=source_key,
        source_cursor=envelope.source_cursor,
        observed_at=envelope.received_at,
        decode_complete=False,
        detail=result.blocker,
    )

    runtime = type(
        "Runtime",
        (),
        {"process_fn": lambda _self, _envelope: (_ for _ in ()).throw(
            AssertionError("out-of-scope recovery row must not be replayed")
        )},
    )()
    resolved = worker._retry_stored_solana_decode_gaps(
        conn,
        runtime,
        source_key=source_key,
        venue=worker.VenueId.BAGS_SOLANA,
        program_id=worker.BAGS_DBC_PROGRAM,
        bags_pool_mints={
            base58_encode(bytes([10]) * 32): base58_encode(bytes([13]) * 32)
        },
    )

    assert resolved == 1
    assert [
        row for row in worker.list_open_gaps(conn, source_key)
        if row["gap_kind"] == "decode_failure"
    ] == []
    assert worker.frontier_gap_is_discardable(conn, source_key) is True
    assert conn.execute("SELECT count(*) FROM op_canonical_events").fetchone()[0] == 0


def test_stored_unsupported_global_bags_event_becomes_discardable(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "bags_solana:ws"
    record_source_failure(
        conn,
        source_key=source_key,
        observed_at="2026-07-26T12:00:00Z",
        detail="fixture disconnect",
    )
    envelope = worker.OpportunitySourceEnvelope(
        source_key=source_key,
        source_provider="solana_rpc_recovery",
        source_cursor="123:sig-unsupported:0",
        venue_id=worker.VenueId.BAGS_SOLANA,
        program_or_contract_id=worker.BAGS_DBC_PROGRAM,
        transaction_id="sig-unsupported",
        block_or_slot=123,
        transaction_index=None,
        event_index=0,
        block_hash=None,
        chain_timestamp="2026-07-26T12:00:01Z",
        received_at="2026-07-26T12:00:01.100000Z",
        confirmation=ConfirmationState.TENTATIVE,
        payload={
            "log_line": "Program data: "
            + base64.b64encode(bytes.fromhex("0123456789abcdef")).decode(),
            "actor_id": "actor-1",
        },
        token_hint=worker.BAGS_DBC_PROGRAM,
    )
    result = worker.process_envelope(
        conn, envelope, identities=worker.VenueIdentityMap()
    )
    assert result.blocker == "UNKNOWN_PROGRAM_OR_CONTRACT_VERSION"
    record_processed_cursor(
        conn,
        source_key=source_key,
        source_cursor=envelope.source_cursor,
        observed_at=envelope.received_at,
        decode_complete=False,
        detail=result.blocker,
    )
    runtime = type(
        "Runtime",
        (),
        {"process_fn": lambda _self, _envelope: (_ for _ in ()).throw(
            AssertionError("legacy global recovery row must not be replayed")
        )},
    )()

    resolved = worker._retry_stored_solana_decode_gaps(
        conn,
        runtime,
        source_key=source_key,
        venue=worker.VenueId.BAGS_SOLANA,
        program_id=worker.BAGS_DBC_PROGRAM,
        bags_pool_mints={"pool-1": "mint-1"},
    )

    assert resolved == 1
    assert [
        row for row in worker.list_open_gaps(conn, source_key)
        if row["gap_kind"] == "decode_failure"
    ] == []
    assert worker.frontier_gap_is_discardable(conn, source_key) is True


def _seed_bags_4663_decode_gap(conn, *, address: str, topic: str | None = None):
    source_key = "bags_robinhood_4663:ws"
    envelope = worker.OpportunitySourceEnvelope(
        source_key=source_key,
        source_provider="eth_getLogs_poll",
        source_cursor="123:0:1",
        venue_id=worker.VenueId.BAGS_ROBINHOOD_4663,
        program_or_contract_id=address,
        transaction_id="0xtx",
        block_or_slot=123,
        transaction_index=0,
        event_index=1,
        block_hash="0xblock",
        chain_timestamp="2026-07-26T12:00:01Z",
        received_at="2026-07-26T12:00:01.100000Z",
        confirmation=ConfirmationState.CONFIRMED,
        payload={
            "log": {
                "address": address,
                "topics": [topic or worker.TOKENS_BOUGHT],
                "data": "0x",
            },
        },
        token_hint=address,
    )
    result = worker.process_envelope(
        conn,
        envelope,
        identities=worker.VenueIdentityMap(),
    )
    assert result.blocker == "UNKNOWN_PROGRAM_OR_CONTRACT_VERSION"
    record_processed_cursor(
        conn,
        source_key=source_key,
        source_cursor=envelope.source_cursor,
        observed_at=envelope.received_at,
        decode_complete=False,
        detail=result.blocker,
    )
    return source_key


def test_stored_untracked_bags_4663_curve_gap_resolves(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    address = "0x1111111111111111111111111111111111111111"
    source_key = _seed_bags_4663_decode_gap(conn, address=address)
    identities = worker.VenueIdentityMap()
    runtime = SimpleNamespace(
        conn=conn,
        now_fn=lambda: "2026-07-26T12:00:02Z",
        mark_recovered=lambda *_args, **_kwargs: None,
    )

    resolved = worker._resolve_stored_untracked_bags_4663_gaps(
        runtime,
        identities,
    )

    assert resolved == 1
    assert worker.list_open_gaps(conn, source_key) == []
    assert worker.load_frontier(conn, source_key).has_open_gap is False


def test_stored_foreign_factory_topic_gap_resolves(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = _seed_bags_4663_decode_gap(
        conn,
        address="0xaba2c582ed7fb655374d80892c140271f1987911",
        topic=worker.TOKEN_CREATED,
    )
    runtime = SimpleNamespace(
        conn=conn,
        now_fn=lambda: "2026-07-26T12:00:02Z",
        mark_recovered=lambda *_args, **_kwargs: None,
    )

    resolved = worker._resolve_stored_untracked_bags_4663_gaps(
        runtime,
        worker.VenueIdentityMap(),
    )

    assert resolved == 1
    assert worker.list_open_gaps(conn, source_key) == []


def test_stored_tracked_bags_4663_curve_gap_stays_open(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    address = "0x1111111111111111111111111111111111111111"
    source_key = _seed_bags_4663_decode_gap(conn, address=address)
    identities = worker.VenueIdentityMap()
    identities.token_by_4663_contract[address] = "0xtoken"
    runtime = SimpleNamespace(
        conn=conn,
        now_fn=lambda: "2026-07-26T12:00:02Z",
        mark_recovered=lambda *_args, **_kwargs: None,
    )

    resolved = worker._resolve_stored_untracked_bags_4663_gaps(
        runtime,
        identities,
    )

    assert resolved == 0
    assert len(worker.list_open_gaps(conn, source_key)) == 1


def test_solana_recovery_skips_transaction_reads_at_incomplete_page_bound(
    monkeypatch, tmp_path
):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "pumpfun_solana:ws"
    record_source_failure(
        conn,
        source_key=source_key,
        observed_at="2026-07-26T12:00:00Z",
        detail="fixture disconnect",
    )
    requested_limits = []

    def rpc(_url, method, params):
        if method == "getSignaturesForAddress":
            limit = params[1]["limit"]
            requested_limits.append(limit)
            rows = [{"signature": f"sig-{index}"} for index in range(11)]
            return rows[:limit]
        if method == "getTransaction":
            raise AssertionError("incomplete index page must not trigger transaction reads")
        raise AssertionError(method)

    class Runtime:
        def submit(self, *_args, **_kwargs):
            raise AssertionError("empty fixture transaction must not submit")

        def drain(self, **_kwargs):
            raise AssertionError("empty fixture transaction must not drain")

        def heartbeat(self, *_args, **_kwargs):
            pass

        def mark_recovered(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(worker, "json_rpc_http", rpc)
    complete = asyncio.run(
        worker._recover_solana_frontier(
            conn,
            Runtime(),
            venue=worker.VenueId.PUMPFUN_SOLANA,
            program_id=worker.PUMPFUN_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
        )
    )

    assert complete is False
    assert requested_limits == [10]
    gaps = worker.list_open_gaps(conn, source_key)
    assert len(gaps) == 1
    assert gaps[0]["gap_kind"] == "enumeration_bound"
    assert gaps[0]["resume_cursor"] == "sig-9"


def test_solana_reconnect_recovers_ordinary_burst_in_one_governed_page(
    monkeypatch, tmp_path
):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "pumpfun_solana:ws"
    conn.execute(
        "INSERT INTO op_raw_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "raw-durable",
            "solana",
            "pumpfun_solana",
            source_key,
            "solana_logs_subscribe",
            "99:durable:0",
            worker.PUMPFUN_PROGRAM,
            "durable",
            99,
            None,
            0,
            None,
            "2026-07-26T12:00:00Z",
            "2026-07-26T12:00:00Z",
            "tentative",
            "{}",
            "payload-durable",
            "fingerprint-durable",
            "2026-07-26T12:00:00Z",
        ),
    )
    record_processed_cursor(
        conn,
        source_key=source_key,
        source_cursor="99:durable:0",
        observed_at="2026-07-26T12:00:00Z",
        decode_complete=True,
    )
    record_source_failure(
        conn,
        source_key=source_key,
        observed_at="2026-07-26T12:00:01Z",
        detail="fixture disconnect",
    )
    requested_limits = []
    transaction_reads = []

    def rpc(_url, method, params):
        if method == "getSignaturesForAddress":
            requested_limits.append(params[1]["limit"])
            rows = [
                {"signature": f"new-{index}", "slot": 100 + index}
                for index in range(25)
            ] + [{"signature": "durable", "slot": 99}]
            return rows[: params[1]["limit"]]
        if method == "getTransaction":
            transaction_reads.append(params[0])
            return {
                "slot": 100,
                "blockTime": 1_785_000_000,
                "meta": {"logMessages": []},
                "transaction": {"message": {"accountKeys": ["actor-1"]}},
            }
        raise AssertionError(method)

    class Runtime:
        recovered = False

        def submit(self, *_args, **_kwargs):
            raise AssertionError("empty fixture transaction must not submit")

        def drain(self, **_kwargs):
            raise AssertionError("empty fixture transaction must not drain")

        def heartbeat(self, *_args, **_kwargs):
            pass

        def mark_recovered(self, _source_key):
            self.recovered = True

    runtime = Runtime()
    monkeypatch.setattr(worker, "json_rpc_http", rpc)

    complete = asyncio.run(
        worker._recover_solana_frontier(
            conn,
            runtime,
            venue=worker.VenueId.PUMPFUN_SOLANA,
            program_id=worker.PUMPFUN_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
        )
    )

    assert complete is True
    assert requested_limits == [100]
    assert transaction_reads == [f"new-{index}" for index in reversed(range(25))]
    assert worker.list_open_gaps(conn, source_key) == []
    assert runtime.recovered is True


def test_solana_bootstrap_recovery_ignores_history_before_gap(monkeypatch, tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "bags_solana:ws"
    record_source_failure(
        conn,
        source_key=source_key,
        observed_at="2026-07-26T12:00:00Z",
        detail="fixture disconnect before first event",
    )
    methods = []

    def rpc(_url, method, _params):
        methods.append(method)
        if method == "getSignaturesForAddress":
            return [{"signature": "historical", "blockTime": 1_785_067_199}]
        raise AssertionError(method)

    class Runtime:
        recovered = False

        def submit(self, *_args, **_kwargs):
            raise AssertionError("pre-gap history must not be replayed")

        def drain(self, **_kwargs):
            raise AssertionError("pre-gap history must not be drained")

        def heartbeat(self, *_args, **_kwargs):
            pass

        def mark_recovered(self, _source_key):
            self.recovered = True

    runtime = Runtime()
    monkeypatch.setattr(worker, "json_rpc_http", rpc)
    complete = asyncio.run(
        worker._recover_solana_frontier(
            conn,
            runtime,
            venue=worker.VenueId.BAGS_SOLANA,
            program_id=worker.BAGS_DBC_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=1,
        )
    )

    assert complete is True
    assert methods == ["getSignaturesForAddress"]
    assert worker.list_open_gaps(conn, source_key) == []
    assert runtime.recovered is True


def test_bags_recovery_extracts_current_anchor_cpi_launch_event(monkeypatch, tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    source_key = "bags_solana:ws"
    record_source_failure(
        conn,
        source_key=source_key,
        observed_at="2026-07-26T12:00:00Z",
        detail="fixture disconnect before first Bags event",
    )
    pool = bytes([10]) * 32
    config = bytes([11]) * 32
    creator = bytes([12]) * 32
    mint = bytes([13]) * 32
    event = (
        bytes.fromhex("e445a52e51cb9a1d")
        + bytes.fromhex("e432f655cb428625")
        + pool
        + config
        + creator
        + mint
        + struct.pack("<BQ", 0, 1_785_067_201)
    )

    def rpc(_url, method, _params):
        if method == "getSignaturesForAddress":
            return (
                []
                if _params[1].get("before")
                else [{"signature": "bags-launch", "blockTime": 1_785_067_201}]
            )
        if method == "getTransaction":
            return {
                "slot": 123,
                "blockTime": 1_785_067_201,
                "meta": {
                    "logMessages": [],
                    "loadedAddresses": {"writable": [], "readonly": []},
                    "innerInstructions": [{
                        "index": 2,
                        "instructions": [{
                            "programId": worker.BAGS_DBC_PROGRAM,
                            "data": base58_encode(event),
                        }],
                    }],
                },
                "transaction": {"message": {"accountKeys": ["bags-creator"]}},
            }
        raise AssertionError(method)

    class Runtime:
        def __init__(self):
            self.submitted = []

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, *, max_items):
            assert max_items == 1

        def heartbeat(self, *_args, **_kwargs):
            pass

        def mark_recovered(self, *_args, **_kwargs):
            pass

    runtime = Runtime()
    monkeypatch.setattr(worker, "json_rpc_http", rpc)

    complete = asyncio.run(
        worker._recover_solana_frontier(
            conn,
            runtime,
            venue=worker.VenueId.BAGS_SOLANA,
            program_id=worker.BAGS_DBC_PROGRAM,
            rpc_url="https://fixture.invalid",
            max_pages=2,
        )
    )

    assert complete is True
    assert len(runtime.submitted) == 1
    envelope = runtime.submitted[0]
    assert envelope.event_index == (2 << 16)
    assert envelope.source_cursor == f"123:bags-launch:{2 << 16}"
    assert envelope.payload["log_line"].startswith("Program data: 5DL2VctChiU")


def test_bags_feed_recovery_accepts_only_the_feed_named_pool():
    pool = bytes([10]) * 32
    config = bytes([11]) * 32
    creator = bytes([12]) * 32
    mint = bytes([13]) * 32
    from sniper.research.memecoin_opportunity.decoders import base58_encode as b58
    pool_key = b58(pool)
    mint_key = b58(mint)
    event = (
        bytes.fromhex("e445a52e51cb9a1d")
        + bytes.fromhex("e432f655cb428625")
        + pool
        + config
        + creator
        + mint
        + struct.pack("<BQ", 0, 1_785_067_201)
    )
    transaction = {
        "slot": 123,
        "blockTime": 1_785_067_201,
        "meta": {
            "loadedAddresses": {"writable": [], "readonly": []},
            "innerInstructions": [{
                "index": 2,
                "instructions": [{
                    "programId": worker.BAGS_DBC_PROGRAM,
                    "data": base58_encode(event),
                }],
            }],
        },
        "transaction": {"message": {"accountKeys": ["bags-creator"]}},
    }
    launch = BagsLaunch(mint_key, "bags-launch", pool_key)

    envelopes = worker._bags_feed_recovery_envelopes(
        launch,
        transaction,
        received_at="2026-07-26T12:00:02Z",
    )

    assert len(envelopes) == 1
    assert envelopes[0].source_key == "opportunity:bags_feed_recovery"
    assert envelopes[0].token_hint == mint_key
    assert envelopes[0].payload["pool_id"] == pool_key
    assert envelopes[0].confirmation is ConfirmationState.CONFIRMED


def test_evm_recovery_runs_only_after_subscription_is_active(tmp_path, monkeypatch):
    stop = asyncio.Event()
    order = []
    passed_abandon = []

    class WebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            order.append("subscribe_sent")

        async def recv(self):
            order.append("subscription_ack")
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x7"})

    async def recover(*_args, abandon_fn=None, **_kwargs):
        order.append("recovery")
        passed_abandon.append(abandon_fn)
        stop.set()

    class Runtime:
        conn = connect(tmp_path / "op.db")

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_recover_evm_frontier", recover)
    monkeypatch.setattr(worker, "_source_heartbeat", lambda *_args, **_kwargs: None)

    abandon = object()
    asyncio.run(worker._evm_lane(
        Runtime(), ws_url="wss://fixture.invalid", rpc_url="https://fixture.invalid",
        recovery_max_pages=1, stop=stop, abandon_fn=abandon,
    ))

    assert order == ["subscribe_sent", "subscription_ack", "recovery"]
    assert passed_abandon == [abandon]


def test_evm_active_subscription_rebases_known_gap_without_rpc_replay(monkeypatch):
    stop = asyncio.Event()
    calls = []

    class WebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x7"})

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return True

        def heartbeat(self, *_args, **_kwargs):
            pass

    async def recover(*_args, **_kwargs):
        calls.append("recover")
        stop.set()

    def abandon(venue, _observed_at):
        calls.append(venue)
        stop.set()

    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_recover_evm_frontier", recover)
    monkeypatch.setattr(worker, "_source_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "frontier_gap_is_discardable", lambda *_args: True)

    asyncio.run(worker._evm_lane(
        Runtime(), ws_url="wss://fixture.invalid", rpc_url="https://fixture.invalid",
        recovery_max_pages=1, stop=stop, abandon_fn=abandon,
    ))

    assert calls == [worker.VenueId.BAGS_ROBINHOOD_4663]


def test_evm_http_poll_range_preserves_native_log_order(monkeypatch):
    logs = [
        {
            "address": worker.PONS_FACTORY,
            "topics": [worker.PONS_TOKEN_LAUNCHED],
            "data": "0x",
            "blockNumber": "0x11",
            "transactionIndex": "0x2",
            "logIndex": "0x3",
            "blockHash": "0xblock11",
            "transactionHash": "0xtx-b",
            "removed": False,
        },
        {
            "address": worker.PONS_FACTORY,
            "topics": [worker.PONS_TOKEN_LAUNCHED],
            "data": "0x",
            "blockNumber": "0x10",
            "transactionIndex": "0x1",
            "logIndex": "0x0",
            "blockHash": "0xblock10",
            "transactionHash": "0xtx-a",
            "removed": False,
        },
    ]

    def batch(_url, calls):
        if calls[0][0] == "eth_getLogs":
            # Only the Pons factory query remains.
            assert len(calls) == 1
            assert all(params[0]["fromBlock"] == "0x10" for _method, params in calls)
            assert all(params[0]["toBlock"] == "0x11" for _method, params in calls)
            return [logs]
        return [
            {"timestamp": "0x64" if params[0] == "0xblock10" else "0x65"}
            for method, params in calls
            if method == "eth_getBlockByHash"
        ]

    class Runtime:
        conn = sqlite3.connect(":memory:")
        settings = SimpleNamespace(queue_max=8)
        submitted = []
        drain_sizes = []
        heartbeats = 0

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, *, max_items):
            assert self.conn.in_transaction is True
            self.drain_sizes.append(max_items)

        def source_gapped(self, source_key):
            assert source_key == "bags_robinhood_4663:ws"
            return False

        def heartbeat(self, source_key, **kwargs):
            assert source_key == "bags_robinhood_4663:ws"
            assert kwargs["detail"]["transport_stale_after_s"] == 60
            self.heartbeats += 1

    runtime = Runtime()
    monkeypatch.setattr(worker, "json_rpc_batch_http", batch)

    summary = asyncio.run(
        _evm_poll_range(
            runtime,
            rpc_url="https://fixture.invalid",
            from_block=16,
            to_block=17,
        )
    )

    assert [item.source_cursor for item in runtime.submitted] == ["16:1:0", "17:2:3"]
    assert [item.source_provider for item in runtime.submitted] == [
        "eth_getLogs_poll",
        "eth_getLogs_poll",
    ]
    assert all(item.confirmation is ConfirmationState.CONFIRMED for item in runtime.submitted)
    assert runtime.drain_sizes == [2]
    assert runtime.heartbeats == 0
    # Both fixture logs are Pons launches. The order assertion remains unchanged.
    assert summary == {
        # the range reports whether it persisted completely; the caller refuses to
        # advance the scan cursor unless it did
        "persisted_completely": True,
        "rejected_submissions": 0,
        "drain_failures": 0,
        # the range proves its own envelopes left the SHARED queue
        "still_queued": 0,
        "events": 2,
        "launches": 2,
        "last_chain_event_at": "1970-01-01T00:01:41Z",
        "last_launch_at": "1970-01-01T00:01:41Z",
        "by_source": {
            "pons_v1_4663:ws": {
                "events": 2,
                "launches": 2,
                "last_chain_event_at": "1970-01-01T00:01:41Z",
                "last_launch_at": "1970-01-01T00:01:41Z",
            }
        },
    }


def test_evm_poll_requests_only_retained_pons_pools(monkeypatch):
    retained_token = "0x1111111111111111111111111111111111111111"
    expired_token = "0x2222222222222222222222222222222222222222"
    pair = "0x3333333333333333333333333333333333333333"
    retained_pool = "0x4444444444444444444444444444444444444444"
    expired_pool = "0x5555555555555555555555555555555555555555"
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE op_launches(venue_id TEXT,token_id TEXT)")
    conn.execute(
        "INSERT INTO op_launches VALUES(?,?)",
        (worker.VenueId.PONS_V1_ROBINHOOD_4663.value, retained_token),
    )
    calls = []

    async def rpc_batch(_governor, _priority, _url, batch_calls):
        calls.extend(batch_calls)
        return [[] for _call in batch_calls]

    class Runtime:
        settings = SimpleNamespace(queue_max=8)

        def __init__(self):
            self.conn = conn

        def submit(self, *_args):
            return True

        def drain(self, **_kwargs):
            pass

    identities = opportunity_sources.VenueIdentityMap()
    identities.pons_pool_tokens = {
        retained_pool: (retained_token, pair),
        expired_pool: (expired_token, pair),
    }
    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)

    summary = asyncio.run(_evm_poll_range(
        Runtime(),
        rpc_url="https://fixture.invalid",
        from_block=16,
        to_block=16,
        identities=identities,
    ))

    stored_pool_calls = [
        params[0]
        for method, params in calls
        if method == "eth_getLogs" and params[0].get("topics") == [[worker.PONS_V3_SWAP]]
    ]
    assert stored_pool_calls == [{
        "fromBlock": "0x10",
        "toBlock": "0x10",
        "address": [retained_pool],
        "topics": [[worker.PONS_V3_SWAP]],
    }]
    assert summary["events"] == 0


def test_evm_poll_rejects_stored_pons_pools_without_launch_table_connection(monkeypatch):
    identities = opportunity_sources.VenueIdentityMap()
    identities.pons_pool_tokens = {
        "0x4444444444444444444444444444444444444444": (
            "0x1111111111111111111111111111111111111111",
            "0x3333333333333333333333333333333333333333",
        ),
    }

    async def rpc_batch(*_args, **_kwargs):
        raise AssertionError("RPC must not start without the launch-table connection")

    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)

    with pytest.raises(RuntimeError, match="launch-table connection"):
        asyncio.run(_evm_poll_range(
            SimpleNamespace(),
            rpc_url="https://fixture.invalid",
            from_block=16,
            to_block=16,
            identities=identities,
        ))


@pytest.mark.parametrize(
    ("queue_max", "expected"),
    ((1, 1), (64, 64), (256, 256), (4_096, 256)),
)
def test_evm_poll_persist_batch_limit_respects_runtime_capacity(queue_max, expected):
    runtime = SimpleNamespace(settings=SimpleNamespace(queue_max=queue_max))

    assert worker._evm_poll_persist_batch_limit(runtime) == expected


@pytest.mark.parametrize(
    "runtime",
    (
        SimpleNamespace(),
        SimpleNamespace(settings=None),
        SimpleNamespace(settings=SimpleNamespace(queue_max="invalid")),
    ),
)
def test_evm_poll_persist_batch_limit_defaults_for_lightweight_runtime(runtime):
    assert worker._evm_poll_persist_batch_limit(runtime) == 256


def test_evm_http_poll_commits_dense_persistence_once_per_batch(tmp_path, monkeypatch):
    logs = [
        {
            "address": worker.PONS_FACTORY,
            "topics": [worker.PONS_TOKEN_LAUNCHED],
            "data": "0x",
            "blockNumber": hex(100 + index),
            "transactionIndex": "0x0",
            "logIndex": "0x0",
            "blockHash": f"0xblock{index}",
            "transactionHash": f"0xtx{index}",
            "removed": False,
        }
        for index in range(300)
    ]

    async def rpc_batch(*_args, **_kwargs):
        return [logs]

    async def block_times(**_kwargs):
        return {log["blockHash"]: 100 + index for index, log in enumerate(logs)}

    conn = connect(tmp_path / "op.db")
    conn.execute("CREATE TABLE persist_probe(source_cursor TEXT PRIMARY KEY)")

    def process(envelope):
        with worker.transaction(conn):
            conn.execute(
                "INSERT INTO persist_probe(source_cursor) VALUES(?)",
                (envelope.source_cursor,),
            )

    runtime = worker.OpportunityCaptureRuntime(
        conn,
        settings=SimpleNamespace(queue_max=4_096),
        process_fn=process,
        now_fn=lambda: "2026-08-05T12:00:00Z",
    )
    commits = []
    conn.set_trace_callback(
        lambda statement: commits.append(statement)
        if statement.strip().upper() == "COMMIT"
        else None
    )
    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)
    monkeypatch.setattr(worker, "_evm_block_times", block_times)

    summary = asyncio.run(
        _evm_poll_range(
            runtime,
            rpc_url="https://fixture.invalid",
            from_block=100,
            to_block=399,
        )
    )

    assert summary["events"] == 300
    assert conn.execute("SELECT count(*) FROM persist_probe").fetchone()[0] == 300
    assert commits == ["COMMIT", "COMMIT"]


def test_evm_poll_yields_between_persistence_batches(monkeypatch):
    logs = [
        {
            "address": worker.PONS_FACTORY,
            "topics": [worker.PONS_TOKEN_LAUNCHED],
            "data": "0x",
            "blockNumber": hex(100 + index),
            "transactionIndex": "0x0",
            "logIndex": "0x0",
            "blockHash": f"0xblock{index}",
            "transactionHash": f"0xtx{index}",
            "removed": False,
        }
        for index in range(2)
    ]
    ready = []

    async def rpc_batch(*_args, **_kwargs):
        return [logs]

    async def block_times(**_kwargs):
        return {log["blockHash"]: 100 + index for index, log in enumerate(logs)}

    class Runtime:
        conn = sqlite3.connect(":memory:")
        settings = SimpleNamespace(queue_max=1)
        drain_states = []

        def submit(self, *_args):
            return True

        def drain(self, **_kwargs):
            self.drain_states.append(bool(ready))

    async def run_poll(runtime):
        marker = asyncio.create_task(_mark_ready())
        await _evm_poll_range(
            runtime,
            rpc_url="https://fixture.invalid",
            from_block=100,
            to_block=101,
        )
        await marker

    async def _mark_ready():
        ready.append(True)

    runtime = Runtime()
    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)
    monkeypatch.setattr(worker, "_evm_block_times", block_times)

    asyncio.run(run_poll(runtime))

    assert runtime.drain_states == [False, True]


def test_evm_http_poll_caps_header_batches_at_provider_safe_size(monkeypatch):
    logs = [
        {
            "address": worker.PONS_FACTORY,
            "topics": [worker.PONS_TOKEN_LAUNCHED],
            "data": "0x",
            "blockNumber": hex(100 + index),
            "transactionIndex": "0x0",
            "logIndex": "0x0",
            "blockHash": f"0xblock{index}",
            "transactionHash": f"0xtx{index}",
            "removed": False,
        }
        for index in range(21)
    ]
    header_batch_sizes = []

    def batch(_url, calls):
        if calls[0][0] == "eth_getLogs":
            return [logs]
        header_batch_sizes.append(len(calls))
        return [
            {"timestamp": hex(int(params[0].removeprefix("0xblock")))}
            for _method, params in calls
        ]

    class Runtime:
        drain_sizes = []

        def submit(self, _source_key, _envelope):
            pass

        def drain(self, *, max_items):
            self.drain_sizes.append(max_items)

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(worker, "json_rpc_batch_http", batch)

    runtime = Runtime()
    asyncio.run(_evm_poll_range(
        runtime,
        rpc_url="https://fixture.invalid",
        from_block=100,
        to_block=120,
    ))

    assert sum(header_batch_sizes) == 21
    assert max(header_batch_sizes) <= 20
    assert runtime.drain_sizes == [21]


def test_evm_block_times_collapses_equal_timestamp_ranges(monkeypatch):
    logs = [
        {"blockNumber": hex(index), "blockHash": f"0xblock{index}"}
        for index in range(100, 200)
    ]
    requested = []

    def batch(_url, calls):
        requested.extend(params[0] for _method, params in calls)
        return [{"timestamp": "0x64"} for _call in calls]

    monkeypatch.setattr(worker, "json_rpc_batch_http", batch)

    block_times = asyncio.run(worker._evm_block_times(
        rpc_url="https://fixture.invalid",
        logs=logs,
        canonical_through_block=199,
    ))

    assert set(block_times.values()) == {100}
    assert len(block_times) == 100
    assert requested == ["0xblock100", "0xblock199"]


def test_evm_block_times_resolves_timestamp_boundaries_exactly(monkeypatch):
    logs = [
        {"blockNumber": hex(index), "blockHash": f"0xblock{index}"}
        for index in range(100, 200)
    ]
    requested = []

    def batch(_url, calls):
        requested.extend(params[0] for _method, params in calls)
        return [
            {"timestamp": hex(int(params[0].removeprefix("0xblock")) // 10)}
            for _method, params in calls
        ]

    monkeypatch.setattr(worker, "json_rpc_batch_http", batch)

    block_times = asyncio.run(worker._evm_block_times(
        rpc_url="https://fixture.invalid",
        logs=logs,
        canonical_through_block=199,
    ))

    assert block_times == {
        f"0xblock{index}": index // 10 for index in range(100, 200)
    }
    assert len(requested) < len(logs)


def test_evm_block_times_never_inferrs_across_unfinalized_blocks(monkeypatch):
    logs = [
        {"blockNumber": hex(index), "blockHash": f"0xblock{index}"}
        for index in range(100, 200)
    ]
    requested = []

    def batch(_url, calls):
        requested.extend(params[0] for _method, params in calls)
        return [{"timestamp": "0x64"} for _call in calls]

    monkeypatch.setattr(worker, "json_rpc_batch_http", batch)

    block_times = asyncio.run(worker._evm_block_times(
        rpc_url="https://fixture.invalid",
        logs=logs,
        canonical_through_block=150,
    ))

    assert set(block_times.values()) == {100}
    assert set(requested) == {
        "0xblock100", "0xblock150",
        *(f"0xblock{index}" for index in range(151, 200)),
    }


def test_evm_http_poll_skips_known_zero_flow_pons_swap(monkeypatch):
    token = "0x1111111111111111111111111111111111111111"
    pair = "0x4444444444444444444444444444444444444444"
    pool = "0x5555555555555555555555555555555555555555"
    actor = "0x6666666666666666666666666666666666666666"
    topic = lambda value: "0x" + "0" * 24 + value[2:]
    zero_flow_swap = {
        "address": pool,
        "topics": [worker.PONS_V3_SWAP, topic(actor), topic(actor)],
        "data": "0x" + encode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            [0, 1, 2**96, 1_000, 0],
        ).hex(),
        "blockNumber": "0x10",
        "transactionIndex": "0x0",
        "logIndex": "0x0",
        "blockHash": "0xblock10",
        "transactionHash": "0xtx10",
        "removed": False,
    }
    calls = []
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE op_launches(venue_id TEXT,token_id TEXT)")
    conn.execute(
        "INSERT INTO op_launches VALUES(?,?)",
        (worker.VenueId.PONS_V1_ROBINHOOD_4663.value, token),
    )

    def batch(_url, batch_calls):
        calls.append(batch_calls)
        assert batch_calls[0][0] == "eth_getLogs"
        return [[], [], [zero_flow_swap]]

    class Runtime:
        submitted = []

        def __init__(self):
            self.conn = conn

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, *, max_items):
            raise AssertionError("zero-flow swap must not be drained")

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            raise AssertionError("zero-flow swap must not update event health")

    identities = opportunity_sources.VenueIdentityMap()
    identities.pons_pool_tokens[pool] = (token, pair)
    monkeypatch.setattr(worker, "json_rpc_batch_http", batch)

    runtime = Runtime()
    summary = asyncio.run(_evm_poll_range(
        runtime,
        rpc_url="https://fixture.invalid",
        from_block=16,
        to_block=16,
        identities=identities,
    ))

    assert len(calls) == 1
    assert runtime.submitted == []
    assert summary["events"] == 0


def test_evm_recovery_skips_known_zero_flow_pons_swap_before_header_fetch(
    tmp_path, monkeypatch
):
    token = "0x1111111111111111111111111111111111111111"
    pair = "0x4444444444444444444444444444444444444444"
    pool = "0x5555555555555555555555555555555555555555"
    actor = "0x6666666666666666666666666666666666666666"
    topic = lambda value: "0x" + "0" * 24 + value[2:]
    zero_flow_swap = {
        "address": pool,
        "topics": [worker.PONS_V3_SWAP, topic(actor), topic(actor)],
        "data": "0x" + encode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            [0, 1, 2**96, 1_000, 0],
        ).hex(),
        "blockNumber": "0x10",
        "transactionIndex": "0x0",
        "logIndex": "0x0",
        "blockHash": "0xblock10",
        "transactionHash": "0xtx10",
        "removed": False,
    }
    calls = []

    def rpc(_url, method, _params):
        calls.append(method)
        if method == "eth_blockNumber":
            return "0x10"
        if method == "eth_getLogs":
            return [zero_flow_swap]
        if method == "eth_getBlockByHash":
            return {"timestamp": "0x64"}
        raise AssertionError(method)

    class Runtime:
        submitted = []
        recovered = []

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, *, max_items):
            raise AssertionError("zero-flow swap must not be drained")

        def mark_recovered(self, source_key):
            self.recovered.append(source_key)

        def heartbeat(self, *_args, **_kwargs):
            pass

    identities = opportunity_sources.VenueIdentityMap()
    identities.pons_pool_tokens[pool] = (token, pair)
    monkeypatch.setattr(worker, "json_rpc_http", rpc)
    monkeypatch.setattr(
        worker,
        "load_frontier",
        lambda *_args: SimpleNamespace(resume_cursor=None, durable_cursor="15:0:0"),
    )
    monkeypatch.setattr(
        worker,
        "_finish_recovery",
        lambda _conn, active_runtime, source_key, **_kwargs:
            active_runtime.mark_recovered(source_key),
    )
    runtime = Runtime()

    asyncio.run(worker._recover_evm_frontier(
        object(),
        runtime,
        rpc_url="https://fixture.invalid",
        max_pages=1,
        venue=worker.VenueId.PONS_V1_ROBINHOOD_4663,
        identities=identities,
    ))

    assert calls == ["eth_blockNumber", "eth_getLogs"]
    assert runtime.submitted == []
    assert runtime.recovered == ["pons_v1_4663:ws"]


def test_evm_websocket_skips_known_zero_flow_pons_swap_before_header_fetch(
    monkeypatch
):
    token = "0x1111111111111111111111111111111111111111"
    pair = "0x4444444444444444444444444444444444444444"
    pool = "0x5555555555555555555555555555555555555555"
    actor = "0x6666666666666666666666666666666666666666"
    topic = lambda value: "0x" + "0" * 24 + value[2:]
    zero_flow_swap = {
        "address": pool,
        "topics": [worker.PONS_V3_SWAP, topic(actor), topic(actor)],
        "data": "0x" + encode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            [0, 1, 2**96, 1_000, 0],
        ).hex(),
        "blockNumber": "0x10",
        "transactionIndex": "0x0",
        "logIndex": "0x0",
        "blockHash": "0xblock10",
        "transactionHash": "0xtx10",
        "removed": False,
    }
    stop = asyncio.Event()

    class WebSocket:
        messages = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            self.messages += 1
            if self.messages == 1:
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x7"})
            stop.set()
            return json.dumps({"params": {"result": zero_flow_swap}})

    class Runtime:
        conn = object()
        submitted = []

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    async def recover(*_args, **_kwargs):
        pass

    rpc_calls = []

    async def rpc(*_args, **_kwargs):
        rpc_calls.append(_args[3])
        return {"timestamp": "0x64"}

    identities = opportunity_sources.VenueIdentityMap()
    identities.pons_pool_tokens[pool] = (token, pair)
    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_recover_evm_frontier", recover)
    monkeypatch.setattr(worker, "_rpc_call", rpc)
    monkeypatch.setattr(worker, "_source_heartbeat", lambda *_args, **_kwargs: None)

    runtime = Runtime()
    asyncio.run(worker._evm_lane(
        runtime,
        ws_url="wss://fixture.invalid",
        rpc_url="https://fixture.invalid",
        recovery_max_pages=1,
        stop=stop,
        identities=identities,
    ))

    assert rpc_calls == []
    assert runtime.submitted == []


def test_evm_http_poll_catches_up_new_pons_pool_with_one_scoped_query(monkeypatch):
    token = "0x1111111111111111111111111111111111111111"
    deployer = "0x2222222222222222222222222222222222222222"
    dex_factory = "0x3333333333333333333333333333333333333333"
    pair = "0x4444444444444444444444444444444444444444"
    pool = "0x5555555555555555555555555555555555555555"
    topic = lambda value: "0x" + "0" * 24 + value[2:]
    base = {
        "blockNumber": "0x10",
        "transactionIndex": "0x1",
        "blockHash": "0xblock10",
        "removed": False,
    }
    launch_log = {
            **base,
            "address": worker.PONS_FACTORY,
            "topics": [worker.PONS_TOKEN_LAUNCHED, topic(token), topic(deployer), topic(dex_factory)],
            "data": "0x" + encode(
                ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256"],
                [pair, pool, 1, 2, 3, 4, 5],
            ).hex(),
            "logIndex": "0x0",
            "transactionHash": "0xlaunch",
        }
    swap_log = {
            **base,
            "address": pool,
            "topics": [worker.PONS_V3_SWAP, topic(deployer), topic(deployer)],
            "data": "0x" + encode(
                ["int256", "int256", "uint160", "uint128", "int24"],
                [-100, 20, 2**96, 1_000, 0],
            ).hex(),
            "logIndex": "0x1",
            "transactionHash": "0xlaunch",
        }
    calls = []

    def rpc(_url, method, params):
        calls.append((method, params))
        assert method == "eth_getLogs"
        assert params[0]["address"] == [pool]
        assert params[0]["topics"] == [[worker.PONS_V3_SWAP]]
        return [swap_log]

    def batch(_url, batch_calls):
        calls.append(("batch", batch_calls))
        if batch_calls[0][0] == "eth_getLogs":
            # The factory query is the only batched call.
            assert len(batch_calls) == 1
            assert batch_calls[0][1][0]["address"] == worker.PONS_FACTORY
            assert batch_calls[0][1][0]["topics"] == [[worker.PONS_TOKEN_LAUNCHED]]
            return [[launch_log]]
        return [{"timestamp": "0x64"} for _call in batch_calls]

    class Runtime:
        submitted = []
        drain_sizes = []

        def submit(self, _source_key, envelope):
            self.submitted.append(envelope)

        def drain(self, *, max_items):
            self.drain_sizes.append(max_items)

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    runtime = Runtime()
    identities = opportunity_sources.VenueIdentityMap()
    monkeypatch.setattr(worker, "json_rpc_http", rpc)
    monkeypatch.setattr(worker, "json_rpc_batch_http", batch)

    summary = asyncio.run(
        _evm_poll_range(
            runtime,
            rpc_url="https://fixture.invalid",
            from_block=16,
            to_block=16,
            identities=identities,
        )
    )

    assert [item[0] for item in calls] == ["batch", "eth_getLogs", "batch"]
    assert [item.venue_id for item in runtime.submitted] == [
        worker.VenueId.PONS_V1_ROBINHOOD_4663,
        worker.VenueId.PONS_V1_ROBINHOOD_4663,
    ]
    assert [item.source_key for item in runtime.submitted] == [
        "pons_v1_4663:ws",
        "pons_v1_4663:ws",
    ]
    assert runtime.drain_sizes == [2]
    assert summary["events"] == 2
    assert summary["by_source"]["pons_v1_4663:ws"]["launches"] == 1


def test_evm_http_poll_warm_starts_recent_history_when_frontier_is_empty(
    tmp_path, monkeypatch
):
    stop = asyncio.Event()
    calls = []
    heartbeats = []

    async def rpc_batch(*_args, **_kwargs):
        return [hex(10_000), {"number": hex(9_999)}]

    async def poll_range(
        _runtime, *, rpc_url, from_block, to_block, rpc_governor=None, identities=None,
        canonical_through_block=None, stage=None, on_progress=None,
    ):
        calls.append((rpc_url, from_block, to_block, rpc_governor, canonical_through_block))
        stop.set()
        return {
            # a healthy poll must declare completeness; the caller fails closed without it
            "persisted_completely": True,
            "rejected_submissions": 0,
            "drain_failures": 0,
            "events": 1,
            "launches": 1,
            "last_launch_at": "2026-07-27T12:26:23Z",
            "by_source": {
                "bags_robinhood_4663:ws": {
                    "events": 1,
                    "launches": 1,
                    "last_launch_at": "2026-07-27T12:26:23Z",
                }
            },
        }

    class Runtime:
        conn = connect(tmp_path / "op.db")

        def mark_recovered(self, _source_key):
            pass

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **kwargs):
            heartbeats.append(kwargs)

    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)
    monkeypatch.setattr(worker, "_evm_poll_range", poll_range)
    Runtime.conn.execute(
        "INSERT INTO op_runtime_status VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "pons_v1_4663:ws", "healthy", None,
            "2026-07-27T12:25:00Z", "2026-07-27T12:26:00Z", 0, 0,
            '{"last_chain_event_at":"2026-07-27T12:25:00Z"}', 1,
        ),
    )

    asyncio.run(
        worker._evm_poll_lane(
            Runtime(),
            rpc_url="https://fixture.invalid",
            poll_s=0.001,
            stop=stop,
            warm_start_blocks=50_000,
            chunk_blocks=5_000,
        )
    )

    assert calls == [("https://fixture.invalid", 0, 999, None, 9_999)]
    assert heartbeats[0]["detail"]["transport_stale_after_s"] == 60
    assert len(heartbeats) == 4
    assert heartbeats[-2]["detail"] == {
        "capture_state": "listening",
        "recent_events": 1,
        "recent_launches": 1,
        "last_launch_at": "2026-07-27T12:26:23Z",
        "warm_start_blocks": 50_000,
        "transport_stale_after_s": 60,
        # Scan lag is the only venue-independent liveness fact for a shared poll: the
        # fixture warm-started at 0 and scanned through 999 against head 10000.
        "head_block": 10_000,
        "scanned_through_block": 999,
        # measured against the FINALIZED head (9,999), because the lane stops there;
        # the raw-head figure is kept alongside for reference
        "finalized_head_block": 9_999,
        "blocks_behind_head": 9_000,
        "blocks_behind_raw_head": 9_001,
        "max_blocks_behind_head": 600,
        "consecutive_poll_failures": 0,
        "poll_failures_total": 0,
    }
    assert heartbeats[-1]["detail"]["expected_event_max_age_s"] == 300
    assert heartbeats[-1]["detail"]["last_chain_event_at"] == "2026-07-27T12:25:00Z"


def test_evm_http_poll_recovers_runtime_only_failure_without_frontier_rebase(
    tmp_path, monkeypatch
):
    stop = asyncio.Event()
    rpc_calls = 0
    calls = []
    abandon_errors = []

    async def rpc_batch(*_args, **_kwargs):
        nonlocal rpc_calls
        rpc_calls += 1
        if rpc_calls == 1:
            raise TimeoutError("transient head timeout")
        stop.set()
        return [hex(100), {"number": hex(99)}]

    async def poll_range(
        _runtime, *, rpc_url, from_block, to_block, rpc_governor=None, identities=None,
        canonical_through_block=None, stage=None, on_progress=None,
    ):
        calls.append((from_block, to_block, canonical_through_block))
        stop.set()
        return {"events": 0, "launches": 0, "last_launch_at": None, "by_source": {},
                "persisted_completely": True, "rejected_submissions": 0, "drain_failures": 0}

    conn = connect(tmp_path / "op.db")
    runtime = worker.OpportunityCaptureRuntime(
        conn,
        settings=type("Settings", (), {"queue_max": 8})(),
        process_fn=lambda _envelope: None,
        now_fn=lambda: "2026-07-27T12:26:23Z",
    )

    def abandon(venue, observed_at):
        try:
            worker.rebase_frontier_after_discard(
                conn,
                f"{venue.value}:ws",
                observed_at=observed_at,
                detail="production-shaped test abandonment",
            )
        except Exception as exc:
            abandon_errors.append(f"{type(exc).__name__}: {exc}")
            raise

    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)
    monkeypatch.setattr(worker, "_evm_poll_range", poll_range)

    asyncio.run(
        worker._evm_poll_lane(
            runtime,
            rpc_url="https://fixture.invalid",
            poll_s=0.001,
            stop=stop,
            abandon_fn=abandon,
            warm_start_blocks=20,
            chunk_blocks=7,
        )
    )

    status = conn.execute(
        "SELECT status,blocker_code FROM op_runtime_status WHERE source_key=?",
        ("bags_robinhood_4663:ws",),
    ).fetchone()
    assert rpc_calls == 2
    # the warm window counts back from the FINALIZED head, not the raw head, so it
    # delivers `warm_start_blocks` POLLABLE blocks instead of one fewer
    assert calls == [(80, 86, 99)]
    assert abandon_errors == []
    assert runtime.source_gapped("bags_robinhood_4663:ws") is False
    assert tuple(status) == ("healthy", None)
    assert conn.execute("SELECT COUNT(*) FROM op_source_frontiers").fetchone()[0] == 0


def test_evm_http_poll_resumes_from_newest_shared_lane_frontier(tmp_path, monkeypatch):
    stop = asyncio.Event()
    calls = []
    conn = connect(tmp_path / "op.db")
    conn.executemany(
        "INSERT INTO op_source_frontiers VALUES(?,?,?,?,?,?,?)",
        (
            (
                "bags_robinhood_4663:ws", "100:0:0", None, "100:0:0", 0, 1,
                "2026-07-29T12:00:00Z",
            ),
            (
                "pons_v1_4663:ws", "500:0:0", None, "500:0:0", 0, 1,
                "2026-07-29T12:00:00Z",
            ),
        ),
    )

    async def rpc_batch(*_args, **_kwargs):
        return [hex(600), {"number": hex(599)}]

    async def poll_range(
        _runtime, *, rpc_url, from_block, to_block, rpc_governor=None, identities=None,
        canonical_through_block=None, stage=None, on_progress=None,
    ):
        calls.append((from_block, to_block, canonical_through_block))
        stop.set()
        return {"events": 0, "launches": 0, "last_launch_at": None, "by_source": {},
                "persisted_completely": True, "rejected_submissions": 0, "drain_failures": 0}

    class Runtime:
        def __init__(self):
            self.conn = conn

        def mark_recovered(self, _source_key):
            pass

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(worker, "_rpc_batch_call", rpc_batch)
    monkeypatch.setattr(worker, "_evm_poll_range", poll_range)

    asyncio.run(
        worker._evm_poll_lane(
            Runtime(),
            rpc_url="https://fixture.invalid",
            poll_s=0.001,
            stop=stop,
        )
    )

    # only FINALIZED blocks are polled now, so the range stops at 599
    assert calls == [(500, 599, 599)]


def test_quiet_subscribed_source_refreshes_transport_heartbeat(monkeypatch):
    stop = asyncio.Event()

    class WebSocket:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})
            raise asyncio.TimeoutError

    async def recover(*_args, **_kwargs):
        pass

    class Runtime:
        conn = object()
        heartbeats = 0

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, *_args, **_kwargs):
            self.heartbeats += 1
            if self.heartbeats == 2:
                stop.set()

    runtime = Runtime()
    monkeypatch.setattr(websockets, "connect", lambda *_args, **_kwargs: WebSocket())
    monkeypatch.setattr(worker, "_recover_solana_frontier", recover)

    asyncio.run(worker._solana_lane(
        runtime, venue=worker.VenueId.PUMPFUN_SOLANA,
        program_id=worker.PUMPFUN_PROGRAM, ws_url="wss://fixture.invalid",
        rpc_url="https://fixture.invalid", recovery_max_pages=1, stop=stop,
    ))

    assert runtime.heartbeats == 2


def test_solana_lane_reports_transport_liveness_while_startup_recovery_runs(monkeypatch):
    """A subscribed socket is live even mid-recovery.

    Recovery is the lowest RPC priority (RECOVERY=3) on a paced, concurrency-1 pool,
    so a startup recovery can run for hours. Awaiting it inline parked the whole
    lane: exactly one heartbeat at subscribe, then silence, while the socket stayed
    open. bags_solana:ws went dark for 5.2h that way on 2026-07-30 with no gap rows.
    Measured against the pre-fix release: 1 heartbeat. After: keeps beating.
    """
    import websockets

    stop = asyncio.Event()
    heartbeats = []
    recovery_running = asyncio.Event()

    class WebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            return None

        async def recv(self):
            if not recovery_running.is_set():
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": 7})
            # A real idle socket suspends; raising instantly would starve the loop.
            await asyncio.sleep(0.05)
            raise asyncio.TimeoutError()

    async def slow_recovery(*_args, **_kwargs):
        recovery_running.set()
        for _ in range(40):
            if len(heartbeats) >= 3:
                break
            await asyncio.sleep(0.02)
        stop.set()

    class Runtime:
        conn = object()

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, source_key, **_kwargs):
            heartbeats.append(source_key)

    monkeypatch.setattr(websockets, "connect", lambda *_a, **_k: WebSocket())
    monkeypatch.setattr(worker, "_recover_solana_frontier", slow_recovery)

    asyncio.run(
        asyncio.wait_for(
            worker._solana_lane(
                Runtime(), venue=worker.VenueId.BAGS_SOLANA,
                program_id="BagsProgram1111111111111111111111111111111",
                ws_url="wss://fixture.invalid", rpc_url="https://fixture.invalid",
                recovery_max_pages=1, stop=stop,
            ),
            timeout=10,
        )
    )

    assert heartbeats.count("bags_solana:ws") >= 3


def test_bags_lane_heartbeats_transport_even_with_no_active_subscriptions(monkeypatch):
    """A connected socket subscribed to nothing is not a dead transport.

    The idle branch used to iterate `subscriptions.values()`, so a lane whose
    subscription set emptied wrote no heartbeat at all and graded itself
    transport-stale — `bags_solana:ws` sat frozen for hours with a healthy feed, no
    open gap and no traceback. Liveness comes from this loop, not from how much work
    it holds. It must still not claim healthy capture while subscribed to nothing.
    """
    stop = asyncio.Event()

    class WebSocket:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, _frame):
            pass

        async def recv(self):
            # Never confirm a subscription: `subscriptions` stays empty throughout.
            self.calls += 1
            if self.calls > 2:
                stop.set()
            raise asyncio.TimeoutError

    class Runtime:
        conn = object()

        def __init__(self):
            self.beats = []

        def source_gapped(self, _source_key):
            return False

        def heartbeat(self, source_key, *_args, **kwargs):
            self.beats.append((source_key, kwargs.get("status"), kwargs.get("blocker_code")))

    runtime = Runtime()
    monkeypatch.setattr(websockets, "connect", lambda *_a, **_k: WebSocket())
    monkeypatch.setattr(worker, "_recover_solana_frontier", _noop_recover)

    asyncio.run(
        _solana_multi_lane(
            runtime,
            ws_url="wss://fixture.invalid",
            rpc_url="https://fixture.invalid",
            recovery_max_pages=1,
            stop=stop,
            block_cache={},
            bags_pool_mints={"Pool1": "Mint1"},
            include_pump=False,
        )
    )

    bags = [b for b in runtime.beats if b[0] == "bags_solana:ws"]
    assert bags, "an idle bags lane must still report transport liveness"
    # ...but it must name the anomaly rather than claiming healthy capture.
    assert all(b[1] == "degraded" for b in bags)
    assert all(b[2] == "BAGS_SUBSCRIPTIONS_EMPTY" for b in bags)
    assert not [b for b in runtime.beats if b[0] == "pumpfun_solana:ws"]


async def _noop_recover(*_args, **_kwargs):
    pass


def test_evm_poll_lane_bounds_a_hung_poll_and_retries_the_same_range(monkeypatch):
    """Drive the real lane: a hung poll must time out, be reported, and retry the range.

    The previous version of this test patched `_evm_poll_range` and then awaited its own
    local coroutine through its own local timeout helper, so the patch was never used and
    the assertion only proved a string existed in the source. It passed with
    `asyncio.wait_for` deleted. This one fails without the bound.
    """
    stop = asyncio.Event()
    seen_ranges = []

    async def hanging_poll(_runtime, *, from_block, to_block, **_kwargs):
        seen_ranges.append((from_block, to_block))
        if len(seen_ranges) >= 2:
            stop.set()
        await asyncio.sleep(3600)          # cooperative hang at an await point

    async def fake_batch(_gov, _prio, _url, calls):
        # eth_blockNumber + finalized header
        return [hex(1_000), {"number": hex(900)}]

    class Runtime:
        conn = sqlite3.connect(":memory:")

        def source_gapped(self, _k):
            return False

        def heartbeat(self, *_a, **_k):
            pass

        def mark_recovered(self, *_a, **_k):
            pass

    monkeypatch.setattr(worker, "_evm_poll_range", hanging_poll)
    monkeypatch.setattr(worker, "_rpc_batch_call", fake_batch)
    monkeypatch.setattr(worker, "load_frontier", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "frontier_gap_is_discardable", lambda *_a, **_k: False)
    monkeypatch.setattr(worker, "_source_heartbeat", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_EVM_POLL_DEADLINE_S", 0.05)

    asyncio.run(
        asyncio.wait_for(
            worker._evm_poll_lane(
                Runtime(),
                rpc_url="https://fixture.invalid",
                poll_s=0.01,
                stop=stop,
                warm_start_blocks=10,
            ),
            timeout=10,
        )
    )

    # The bound fired twice and handed back the SAME range each time -- no chain
    # evidence is skipped by a timeout.
    assert len(seen_ranges) >= 2, "a hung poll must be bounded, not awaited forever"
    assert seen_ranges[0] == seen_ranges[1], f"range must be retried, got {seen_ranges[:2]}"


def test_evm_poll_failure_log_names_the_range_and_stage(capsys, monkeypatch):
    """`4663 poll failed: TimeoutError:` with an empty message identifies nothing.

    A timeout carries no string, so the handler has to supply the range, the elapsed
    time and the phase itself. Range and duration alone cannot distinguish an RPC wait
    from header resolution from the synchronous decode/persist section -- and only the
    last of those is beyond the deadline's reach, so the phase is what makes the log
    actionable rather than merely narrower.
    """
    stop = asyncio.Event()

    async def hanging_poll(_runtime, *, from_block, to_block, stage=None, **_kwargs):
        if stage is not None:
            stage[:] = ["resolve_headers"]      # hang in a real, named phase
        stop.set()
        await asyncio.sleep(3600)

    async def fake_batch(_gov, _prio, _url, calls):
        return [hex(1_000), {"number": hex(900)}]

    class Runtime:
        conn = sqlite3.connect(":memory:")

        def source_gapped(self, _k):
            return False

        def heartbeat(self, *_a, **_k):
            pass

        def mark_recovered(self, *_a, **_k):
            pass

    monkeypatch.setattr(worker, "_evm_poll_range", hanging_poll)
    monkeypatch.setattr(worker, "_rpc_batch_call", fake_batch)
    monkeypatch.setattr(worker, "load_frontier", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "frontier_gap_is_discardable", lambda *_a, **_k: False)
    monkeypatch.setattr(worker, "_source_heartbeat", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "_EVM_POLL_DEADLINE_S", 0.05)

    asyncio.run(
        asyncio.wait_for(
            worker._evm_poll_lane(
                Runtime(), rpc_url="https://fixture.invalid", poll_s=0.01,
                stop=stop, warm_start_blocks=10,
            ),
            timeout=10,
        )
    )
    err = capsys.readouterr().err
    assert "4663 poll" in err
    assert "blocks=" in err, f"the log must name the range it hung on: {err!r}"
    assert "elapsed_s=" in err, f"the log must name how long it hung: {err!r}"
    assert "stage=resolve_headers" in err, (
        f"the log must name the phase it hung in, not just the range: {err!r}"
    )


def test_evm_poll_range_records_its_phases():
    """The stage recorder must track the real phases, not be decorative.

    Callers that pass no recorder must be unaffected.
    """
    import inspect as _inspect

    source = _inspect.getsource(worker._evm_poll_range)
    for phase in ("fetch_logs", "decode_discovery", "resolve_headers", "decode_persist"):
        assert f'_mark("{phase}")' in source, f"phase {phase} is not recorded"
    # The synchronous section the deadline cannot interrupt must be one of them.
    assert source.index('_mark("fetch_logs")') < source.index('_mark("decode_persist")')
    assert "stage: list[str] | None = None" in source, "recorder must stay optional"


def test_long_poll_stays_alive_while_it_makes_progress(monkeypatch):
    """A poll longer than the transport bound must not mark a working lane down.

    The lane wrote a heartbeat only at the START of a poll, so a backlog burn or a quiet
    range that decodes nothing aged past the 60s bound and rendered `capture=down` while
    the lane was healthy.
    """
    stop = asyncio.Event()
    beats = []

    async def progressing_poll(_runtime, *, from_block, to_block, on_progress=None, **_kw):
        for _ in range(3):                       # three units of real forward progress
            if on_progress is not None:
                on_progress()
            await asyncio.sleep(0)
        stop.set()
        return {"by_source": {}}

    async def fake_batch(_gov, _prio, _url, calls):
        return [hex(1_000), {"number": hex(900)}]

    class Runtime:
        conn = sqlite3.connect(":memory:")

        def source_gapped(self, _k):
            return False

        def heartbeat(self, *_a, **_k):
            pass

        def mark_recovered(self, *_a, **_k):
            pass

    monkeypatch.setattr(worker, "_evm_poll_range", progressing_poll)
    monkeypatch.setattr(worker, "_rpc_batch_call", fake_batch)
    monkeypatch.setattr(worker, "load_frontier", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "frontier_gap_is_discardable", lambda *_a, **_k: False)
    monkeypatch.setattr(worker, "_source_heartbeat", lambda _r, k: beats.append(k))
    # Disable the write throttle so this measures the progress coupling, not the
    # rate limiter. Patching time.monotonic globally would break the event loop.
    monkeypatch.setattr(worker, "_EVM_PROGRESS_HEARTBEAT_MIN_S", 0.0)

    asyncio.run(
        asyncio.wait_for(
            worker._evm_poll_lane(
                Runtime(), rpc_url="https://fixture.invalid", poll_s=0.01,
                stop=stop, warm_start_blocks=10,
            ),
            timeout=10,
        )
    )

    pons = [k for k in beats if k == "pons_v1_4663:ws"]
    assert len(pons) > 1, (
        "a progressing poll must refresh the heartbeat more than once, "
        f"got {len(pons)} for beats={beats}"
    )


def test_a_stalled_poll_writes_no_progress_heartbeat(monkeypatch):
    """The anti-masking guarantee: no progress means no heartbeat.

    A free-running timer would keep a hung poll looking alive, which is precisely the
    failure this whole line of work exists to surface. Heartbeats must come only from
    completed work.
    """
    stop = asyncio.Event()
    beats = []

    async def hanging_poll(_runtime, *, from_block, to_block, on_progress=None, **_kw):
        stop.set()
        await asyncio.sleep(3600)                # never calls on_progress

    async def fake_batch(_gov, _prio, _url, calls):
        return [hex(1_000), {"number": hex(900)}]

    class Runtime:
        conn = sqlite3.connect(":memory:")

        def source_gapped(self, _k):
            return False

        def heartbeat(self, *_a, **_k):
            pass

        def mark_recovered(self, *_a, **_k):
            pass

    monkeypatch.setattr(worker, "_evm_poll_range", hanging_poll)
    monkeypatch.setattr(worker, "_rpc_batch_call", fake_batch)
    monkeypatch.setattr(worker, "load_frontier", lambda *_a, **_k: None)
    monkeypatch.setattr(worker, "frontier_gap_is_discardable", lambda *_a, **_k: False)
    monkeypatch.setattr(worker, "_EVM_POLL_DEADLINE_S", 0.05)
    started = []
    monkeypatch.setattr(worker, "_source_heartbeat", lambda _r, k: started.append(k))

    asyncio.run(
        asyncio.wait_for(
            worker._evm_poll_lane(
                Runtime(), rpc_url="https://fixture.invalid", poll_s=0.01,
                stop=stop, warm_start_blocks=10,
            ),
            timeout=10,
        )
    )
    # The poll-start beat is expected; what must NOT happen is a beat with no progress
    # behind it. `hanging_poll` never calls on_progress, so nothing beyond the start
    # marker may appear per source.
    assert started.count("pons_v1_4663:ws") <= 1, (
        f"a stalled poll must not keep refreshing its heartbeat: {started}"
    )

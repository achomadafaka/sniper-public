#!/usr/bin/env python3
"""Default-OFF event-driven launch-opportunity capture worker.

This process owns only `memecoin_opportunity.db`. It has no signer, custody,
submission, funded-position, or V6 imports.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
_BAGS_WS_SUBSCRIPTION_CAP = 50
_BAGS_WS_SHARD_COUNT = 4
_EVM_HEADER_BATCH_LIMIT = 20
_EVM_POLL_BLOCK_LIMIT = 1_000
_EVM_PERSIST_BATCH_SIZE = 256
# One bounded poll covers at most _EVM_POLL_BLOCK_LIMIT blocks. The deadline is a hang
# bound, not a throughput target. On expiry the same range is retried on the next
# iteration -- no chain evidence is skipped.
_EVM_POLL_DEADLINE_S = 300.0
# Minimum gap between progress-driven heartbeat writes. Bounds DB churn on a busy range
# without decoupling the heartbeat from progress: no progress still means no write.
_EVM_PROGRESS_HEARTBEAT_MIN_S = 1.0
_EVM_TRANSPORT_STALE_AFTER_S = 60
_SOLANA_TRANSPORT_STALE_AFTER_S = 60
# Event-age bound for the sources whose launch flow is high cadence: pons_v1_4663 and
# pumpfun_solana. It grades a lane that holds a live socket and receives nothing.
_EXPECTED_EVENT_MAX_AGE_S = 300
# 4663 runs at roughly 10 blocks/s, so 600 blocks is about a minute of scan lag. A v5
# decision fires at launch age 10-60s and its paper request expires in 30s, so evidence
# that arrives more than a minute late cannot be acted on. This grades the lane; it does
# not throttle or skip anything.
_EVM_MAX_BLOCKS_BEHIND_HEAD = 600
# The Bags coverage loop drives this lane on a 5s poll, so 60s is ~12 missed passes
# before it grades degraded. Matches the socket lanes rather than the 120s default the
# projection applies to a source that declares nothing.
_BAGS_RECOVERY_STALE_AFTER_S = 60
# The lane heartbeats once per coverage pass, so its bound has to follow the poll it is
# driven by. A fixed 60s silently recreated the exact false-stale it was meant to remove
# as soon as MEMECOIN_OPPORTUNITY_BAGS_COVERAGE_INTERVAL_S was set above 60 -- and that
# knob is read straight from the environment with no validation.
_BAGS_RECOVERY_MISSED_PASSES = 12
_RUNTIME_TIMING_KEYS = (
    "main_loop",
    "pump_lane",
    "evm_lane",
    "quote_lane",
    "paper_lifecycle",
    "maintenance",
)


class _RuntimeTimingMetrics:
    """Keep one fixed-size timing result for each runtime lane."""

    def __init__(self) -> None:
        self._values = {
            key: {
                "last_duration_ms": 0,
                "max_duration_ms": 0,
                "runs": 0,
                "over_limit_count": 0,
                "last_over_limit": False,
                "limit_ms": 0,
            }
            for key in _RUNTIME_TIMING_KEYS
        }

    def observe(self, key: str, duration_s: float, *, limit_s: float) -> None:
        if key not in self._values:
            raise ValueError(f"unknown runtime timing key: {key}")
        duration_ms = max(0, int(round(float(duration_s) * 1_000)))
        limit_ms = max(1, int(round(float(limit_s) * 1_000)))
        item = self._values[key]
        over_limit = duration_ms > limit_ms
        item["last_duration_ms"] = duration_ms
        item["max_duration_ms"] = max(int(item["max_duration_ms"]), duration_ms)
        item["runs"] = int(item["runs"]) + 1
        item["over_limit_count"] = int(item["over_limit_count"]) + int(over_limit)
        item["last_over_limit"] = over_limit
        item["limit_ms"] = limit_ms

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {key: dict(value) for key, value in self._values.items()}


def _bags_recovery_stale_after_s(interval_s: float) -> float:
    """Bound the recovery lane at several missed passes, never below the socket bound."""
    try:
        interval = float(interval_s)
    except (TypeError, ValueError):
        return float(_BAGS_RECOVERY_STALE_AFTER_S)
    if not math.isfinite(interval) or interval <= 0:
        return float(_BAGS_RECOVERY_STALE_AFTER_S)
    return max(_BAGS_RECOVERY_STALE_AFTER_S, interval * _BAGS_RECOVERY_MISSED_PASSES)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.research.memecoin_opportunity.bags_4663 import (
    CLAIMERS_UPDATED,
    MIGRATED,
    TOKEN_CREATED,
    TOKENS_BOUGHT,
    TOKENS_SOLD,
    is_untracked_bags_4663_curve_log,
)
from sniper.research.memecoin_opportunity.bags_solana import (
    BAGS_DBC_PROGRAM,
    decode_bags_solana_event,
    is_known_irrelevant_bags_solana_event,
)
from sniper.research.memecoin_opportunity.bags_coverage import (
    BagsForwardCoverage,
    fetch_bags_launch_feed,
    reconcile_bags_coverage,
)
from sniper.research.memecoin_opportunity.continuity import (
    record_processed_cursor,
    record_source_failure,
)
from sniper.research.memecoin_opportunity.frontier import (
    FrontierBatch,
    GapKind,
    apply_frontier_batch,
    frontier_gap_is_discardable,
    list_open_gaps,
    load_frontier,
    rebase_frontier_after_discard,
    resolve_gap,
)
from sniper.research.memecoin_opportunity.decoders import DecodeBlocker, DecodedVenueEvent
from sniper.research.memecoin_opportunity.directional import DirectionalVerdict, RadarZone
from sniper.research.memecoin_opportunity.curve_math import quote_atomic_for_usd
from sniper.research.memecoin_opportunity.engine import OpportunityEngine, RemoteRoundTripProbe
from sniper.research.memecoin_opportunity.evm_probe import OpportunityEvmProbe
from sniper.research.memecoin_opportunity.identity import content_fingerprint
from sniper.research.memecoin_opportunity.landmarks import (
    LaunchSnapshot,
    OutOfOrderObservation,
)
from sniper.research.memecoin_opportunity.market_inputs import NativeUsdPriceFeed
from sniper.research.memecoin_opportunity.paper_adapter import (
    QuoteDeferred,
    QuoteRejected,
    drive_paper_adapter,
)
from sniper.research.memecoin_opportunity.paper_lifecycle import (
    activate_paper_lifecycle,
    drive_paper_lifecycle,
    append_bound_paper_completion,
)
from sniper.research.memecoin_opportunity.paper_sampler import (
    SOURCE_KEY as PAPER_SAMPLER_SOURCE_KEY,
    PaperSampler,
)
from sniper.research.memecoin_opportunity.paper_lifecycle_v3 import (
    activate_adaptive_lifecycle,
    drive_adaptive_lifecycle,
)
from sniper.research.memecoin_opportunity.paper_lifecycle_realistic_v3 import (
    ActivationNotAuthorised,
    BaselineAgeNotPreregistered,
    activation_authorised,
    activate_realistic_v3,
    drive_realistic_v3,
)
from sniper.research.memecoin_opportunity.pumpfun import (
    PUMPFUN_PROGRAM,
    is_known_irrelevant_pumpfun_event,
)
from sniper.research.memecoin_opportunity.pons_4663 import (
    PONS_FACTORY,
    TOKEN_LAUNCHED as PONS_TOKEN_LAUNCHED,
    V3_SWAP as PONS_V3_SWAP,
    decode_pons_4663_log,
    is_known_irrelevant_pons_swap,
    remember_pool as remember_pons_pool,
)
from sniper.research.memecoin_opportunity.reconciliation import reconcile_confirmation
from sniper.research.memecoin_opportunity.insights import connect_insights
from sniper.research.memecoin_opportunity.retention import (
    discard_launch_episodes,
    maintain_storage,
)
from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
from sniper.research.memecoin_opportunity.solana_probe import (
    NATIVE_SOL_MINT,
    OpportunitySolanaProbe,
)
from sniper.research.memecoin_opportunity.store import (
    advance_evm_scan_cursor,
    connect,
    load_evm_scan_cursor,
    transaction,
)
from sniper.research.memecoin_opportunity.state import (
    LaunchState,
    add_feature_derivatives,
    launch_state_from_json,
    liquidity_removed_fraction_bps,
    point_in_time_features,
)
from sniper.research.memecoin_opportunity.types import (
    ConfirmationState,
    EventKind,
    LifecycleState,
    VenueId,
)
from sniper.research.memecoin_opportunity.venue_policy import (
    active_capture_venues,
    is_active_capture_venue,
)
from sniper.services.memecoin_opportunity_live import (
    bounded_solana_recovery,
    evm_logs_subscribe_frame,
    json_rpc_batch_http,
    json_rpc_http,
    make_bags_4663_quote_provider,
    make_bags_4663_state_enricher,
    make_pons_4663_quote_provider,
    parse_evm_log_notification,
    parse_solana_log_notification,
    program_cpi_event_logs,
    program_data_logs,
    solana_program_subscribe_frame,
)
from sniper.services.memecoin_opportunity_runtime import (
    OpportunityCaptureRuntime,
    require_capture_enabled,
)
from sniper.services.memecoin_opportunity_rpc import OpportunityRpcGovernor, RpcPriority
from sniper.services.memecoin_opportunity_sources import (
    OpportunitySourceEnvelope,
    VenueIdentityMap,
    process_envelope,
)
from sniper.settings.memecoin_opportunity import load_memecoin_opportunity_settings
from sniper.util.envfiles import load_env_files
from sniper.util.clock import real_utc_now_iso


def _evm_poll_persist_batch_limit(runtime) -> int:
    queue_max = getattr(
        getattr(runtime, "settings", None),
        "queue_max",
        _EVM_PERSIST_BATCH_SIZE,
    )
    try:
        queue_max = int(queue_max)
    except (TypeError, ValueError):
        queue_max = _EVM_PERSIST_BATCH_SIZE
    return max(1, min(_EVM_PERSIST_BATCH_SIZE, queue_max))


def _drain_evm_poll_batch(runtime, pending_items: int) -> int:
    """Drain a batch and return how many envelopes FAILED to process.

    `drain` records a failure as a gapped source and keeps going, so the count is the
    only signal that a batch did not persist completely.
    """
    if pending_items <= 0:
        return 0
    with transaction(getattr(runtime, "conn", None)):
        runtime.drain(max_items=pending_items)
    return int(getattr(runtime, "drain_failures", 0) or 0)


def _queued_range_items(runtime, range_keys) -> int:
    """How many of THIS range's envelopes are still sitting in the shared queue."""
    if not range_keys:
        return 0
    queue = getattr(runtime, "queue", None) or ()
    return sum(
        1 for source_key, envelope in queue
        if (source_key, getattr(envelope, "source_cursor", None)) in range_keys
    )


def _drain_until_range_is_clear(runtime, range_keys) -> int:
    """Drain until this range's envelopes have left the queue. Return failure count.

    The queue is SHARED with other source tasks and `drain` takes from the oldest side,
    so draining `pending_items` can consume foreign envelopes and leave this range's
    events queued. The caller would then advance the scan cursor over events that are
    still volatile, and a crash would lose them.
    """
    failures = 0
    guard = 0
    while _queued_range_items(runtime, range_keys):
        remaining = _queued_range_items(runtime, range_keys)
        queue_len = len(getattr(runtime, "queue", ()) or ())
        if not queue_len:
            break
        failures += _drain_evm_poll_batch(runtime, queue_len)
        guard += 1
        if _queued_range_items(runtime, range_keys) >= remaining or guard > 16:
            # No progress, or a runaway: stop and let completeness report the truth.
            break
    return failures


def _retained_pons_pool_tokens(
    runtime,
    identities: VenueIdentityMap,
) -> dict[str, tuple[str, str]]:
    pools = dict(identities.pons_pool_tokens)
    if not pools:
        return {}
    conn = getattr(runtime, "conn", None)
    if conn is None or not hasattr(conn, "execute"):
        raise RuntimeError("Pons pool filter requires a launch-table connection")
    retained_tokens = {
        str(row[0]).lower()
        for row in conn.execute(
            "SELECT token_id FROM op_launches WHERE venue_id=?",
            (VenueId.PONS_V1_ROBINHOOD_4663.value,),
        ).fetchall()
    }
    return {
        pool_id: tokens
        for pool_id, tokens in pools.items()
        if str(tokens[0]).lower() in retained_tokens
    }


def _resolve_stored_untracked_bags_4663_gaps(
    runtime,
    identities: VenueIdentityMap,
) -> int:
    """Resolve old curve logs that the current decoder rejects before decode."""
    source_key = "bags_robinhood_4663:ws"
    current = load_frontier(runtime.conn, source_key)
    if current is None or not current.has_open_gap:
        return 0
    resolved_ids = []
    for gap in list_open_gaps(runtime.conn, source_key):
        if (
            gap["gap_kind"] != GapKind.DECODE_FAILURE.value
            or gap["detail"] != DecodeBlocker.UNKNOWN_VERSION.value
        ):
            continue
        row = runtime.conn.execute(
            "SELECT raw_payload_json FROM op_raw_events "
            "WHERE source_key=? AND source_cursor=?",
            (source_key, gap["resume_cursor"]),
        ).fetchone()
        if row is None:
            continue
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        log = payload.get("log") if isinstance(payload, dict) else None
        if isinstance(log, dict) and is_untracked_bags_4663_curve_log(
            log,
            identities.token_by_4663_contract,
        ):
            resolved_ids.append(str(gap["gap_id"]))
    if not resolved_ids:
        return 0
    target_cursor = current.latest_observed_cursor or current.durable_cursor
    if target_cursor is None:
        return 0
    updated = apply_frontier_batch(
        runtime.conn,
        FrontierBatch(
            source_key=source_key,
            expected_version=current.version,
            previous_durable_cursor=current.durable_cursor,
            newest_observed_cursor=target_cursor,
            contiguous_durable_cursor=target_cursor,
            resume_cursor=current.resume_cursor,
            reached_previous_frontier=True,
            enumeration_complete=True,
            decode_complete=True,
            observed_at=runtime.now_fn(),
            resolved_gap_ids=tuple(resolved_ids),
        ),
    )
    if not updated.has_open_gap:
        runtime.mark_recovered(source_key)
    return len(resolved_ids)


async def _rpc_call(governor, priority, rpc_url, method, params):
    if governor is None:
        return await asyncio.to_thread(json_rpc_http, rpc_url, method, params)
    return await governor.call(
        priority,
        json_rpc_http,
        rpc_url,
        method,
        params,
        retry_rate_limits=False,
    )


async def _rpc_batch_call(governor, priority, rpc_url, calls):
    if not calls:
        return []
    if governor is None:
        return await asyncio.to_thread(json_rpc_batch_http, rpc_url, calls)
    return await governor.call(
        priority,
        json_rpc_batch_http,
        rpc_url,
        calls,
        retry_rate_limits=False,
    )


async def _transition_ingestion_tasks(
    *,
    pause_ingestion,
    ingestion_paused,
    source_tasks,
    start_source_tasks,
    tracked_tasks,
):
    if pause_ingestion and not ingestion_paused:
        for task in source_tasks:
            task.cancel()
        await asyncio.gather(*source_tasks, return_exceptions=True)
        return True, []
    if not pause_ingestion and ingestion_paused:
        restarted = list(start_source_tasks())
        tracked_tasks.extend(restarted)
        return False, restarted
    return ingestion_paused, source_tasks


async def _bags_coverage_loop(
    conn,
    *,
    api_key: str,
    interval_s: float,
    stop,
    pool_mints: dict[str, str] | None = None,
    runtime=None,
    rpc_url: str = "",
    rpc_governor=None,
) -> None:
    """Discover Bags pools and audit them; persist only exact chain transactions."""
    coverage = BagsForwardCoverage()
    while not stop.is_set():
        now = real_utc_now_iso()
        # The recovery lane previously only got a status row when it actually queued
        # work, so an idle lane -- the normal state, since Bags rarely needs backfill --
        # aged past the default 120s bound and graded itself degraded, holding the whole
        # board stale for no fault. Heartbeat means "this lane is alive" everywhere else
        # in this worker; `last_event_at` carries "it last recovered something" and is
        # preserved by COALESCE. Written once per pass, before the feed call, because the
        # lane is alive whether or not the provider answers.
        upsert_runtime_status(
            conn,
            source_key="opportunity:bags_feed_recovery",
            status="healthy",
            blocker_code=None,
            heartbeat_at=now,
            detail={
                "transport_stale_after_s": _bags_recovery_stale_after_s(interval_s)
            },
        )
        if not api_key:
            detail = reconcile_bags_coverage((), set(), configured=False)
            upsert_runtime_status(
                conn,
                source_key="opportunity:bags_coverage",
                status="unmeasured",
                blocker_code=None,
                heartbeat_at=now,
                detail=detail,
            )
        else:
            try:
                feed = await asyncio.to_thread(fetch_bags_launch_feed, api_key)
                if pool_mints is not None:
                    pool_mints.clear()
                    pool_mints.update(
                        {
                            str(launch.pool_key): launch.token_mint
                            for launch in feed
                            if launch.pool_key
                        }
                    )
                captured = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT token_id FROM op_launches WHERE venue_id='bags_solana'"
                    ).fetchall()
                }
                audited_feed, detail = coverage.observe(feed, captured)
                recovery_queued = 0
                recovery_error = None
                missing_launches = [
                    launch for launch in audited_feed
                    if launch.token_mint not in captured
                    and launch.launch_signature
                    and launch.pool_key
                ][:2]
                if runtime is not None and rpc_url and missing_launches:
                    try:
                        transactions = await _fetch_solana_transactions(
                            rpc_url,
                            [str(launch.launch_signature) for launch in missing_launches],
                            rpc_governor=rpc_governor,
                        )
                        for launch, transaction in zip(missing_launches, transactions):
                            for envelope in _bags_feed_recovery_envelopes(
                                launch,
                                transaction,
                                received_at=real_utc_now_iso(),
                            ):
                                recovery_queued += int(
                                    runtime.submit(envelope.source_key, envelope)
                                )
                    except Exception as exc:
                        recovery_error = type(exc).__name__
                detail["recovery_queued"] = recovery_queued
                if recovery_error:
                    detail["recovery_error_type"] = recovery_error
                gap = detail["coverage_status"] == "coverage_gap"
                upsert_runtime_status(
                    conn,
                    source_key="opportunity:bags_coverage",
                    status="degraded" if gap else "healthy",
                    blocker_code="BAGS_FEED_COVERAGE_GAP" if gap else None,
                    heartbeat_at=real_utc_now_iso(),
                    detail=detail,
                )
            except BaseException as exc:
                upsert_runtime_status(
                    conn,
                    source_key="opportunity:bags_coverage",
                    status="unmeasured",
                    blocker_code=None,
                    heartbeat_at=real_utc_now_iso(),
                    detail={
                        "coverage_status": "provider_unavailable",
                        "error_type": type(exc).__name__,
                    },
                )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


def _chain_iso(timestamp_s: int | float | None, fallback: str) -> str:
    if timestamp_s is None:
        return fallback
    return datetime.fromtimestamp(float(timestamp_s), timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_chunks(values, *, size: int = 3):
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return tuple(tuple(values[start : start + size]) for start in range(0, len(values), size))


def _bags_pool_shard(pool: str, shard_count: int) -> int:
    if shard_count <= 0:
        raise ValueError("Bags shard count must be positive")
    digest = hashlib.sha256(pool.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % shard_count


def _bags_feed_recovery_envelopes(
    launch,
    transaction,
    *,
    received_at: str,
) -> tuple[OpportunitySourceEnvelope, ...]:
    """Convert one Bags feed receipt into chain-verified launch facts only."""
    if (
        not launch.launch_signature
        or not launch.pool_key
        or not isinstance(transaction, dict)
    ):
        return ()
    keys = (((transaction.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
    first = keys[0] if keys else None
    actor = first.get("pubkey") if isinstance(first, dict) else first
    envelopes = []
    for event_index, log_line in program_cpi_event_logs(transaction, BAGS_DBC_PROGRAM):
        decoded = decode_bags_solana_event(
            BAGS_DBC_PROGRAM,
            log_line,
            token_by_pool={launch.pool_key: launch.token_mint},
            actor_id=str(actor) if actor else None,
        )
        if (
            decoded.blocker is not None
            or decoded.curve_id != launch.pool_key
            or decoded.token_id != launch.token_mint
        ):
            continue
        slot = int(transaction["slot"])
        envelopes.append(
            OpportunitySourceEnvelope(
                source_key="opportunity:bags_feed_recovery",
                source_provider="bags_api_chain_recovery",
                source_cursor=f"{slot}:{launch.launch_signature}:{event_index}",
                venue_id=VenueId.BAGS_SOLANA,
                program_or_contract_id=BAGS_DBC_PROGRAM,
                transaction_id=launch.launch_signature,
                block_or_slot=slot,
                transaction_index=None,
                event_index=event_index,
                block_hash=None,
                chain_timestamp=_chain_iso(transaction.get("blockTime"), received_at),
                received_at=received_at,
                confirmation=ConfirmationState.CONFIRMED,
                payload={
                    "log_line": log_line,
                    "actor_id": actor,
                    "pool_id": launch.pool_key,
                },
                token_hint=launch.token_mint,
            )
        )
    return tuple(envelopes)


async def _fetch_solana_transactions(rpc_url, signatures, *, rpc_governor=None):
    def params(signature):
        return [
            signature,
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 1,
            },
        ]

    if rpc_governor is None:
        return [
            await _rpc_call(None, RpcPriority.RECOVERY, rpc_url, "getTransaction", params(signature))
            for signature in signatures
        ]

    transactions = []
    batch_supported = True
    for chunk in _bounded_chunks(signatures):
        if batch_supported:
            try:
                transactions.extend(await rpc_governor.call(
                    RpcPriority.RECOVERY,
                    json_rpc_batch_http,
                    rpc_url,
                    [("getTransaction", params(signature)) for signature in chunk],
                ))
                continue
            except HTTPError as exc:
                if exc.code not in {400, 403, 405, 413}:
                    raise
                batch_supported = False
        for signature in chunk:
            transactions.append(await _rpc_call(
                rpc_governor,
                RpcPriority.RECOVERY,
                rpc_url,
                "getTransaction",
                params(signature),
            ))
    return transactions


def _solana_order_from_block(block, signature: str) -> tuple[int, str, int | None]:
    if not isinstance(block, dict) or not block.get("blockhash"):
        raise ValueError("Solana block identity unavailable")
    signatures = list(block.get("signatures") or [])
    try:
        transaction_index = signatures.index(signature)
    except ValueError as exc:
        raise ValueError("signature absent from Solana block") from exc
    return transaction_index, str(block["blockhash"]), block.get("blockTime")


def _ws_from_http(value: str) -> str:
    if value.startswith("https://"):
        return "wss://" + value[8:]
    if value.startswith("http://"):
        return "ws://" + value[7:]
    return value


def _decoded_from_json(payload: dict[str, object]) -> DecodedVenueEvent:
    values = dict(payload)
    values["event_kind"] = EventKind(values["event_kind"]) if values.get("event_kind") else None
    values["lifecycle"] = LifecycleState(values["lifecycle"])
    values["blocker"] = DecodeBlocker(values["blocker"]) if values.get("blocker") else None
    return DecodedVenueEvent(**values)


def _state_from_json(payload: str) -> LaunchState:
    return launch_state_from_json(payload)


def restore_pipeline(conn, engine: OpportunityEngine, identities: VenueIdentityMap) -> None:
    engine.states.clear()
    engine.last_observations.clear()
    engine.last_landmark_states.clear()
    engine.prior_interval_features.clear()
    engine.last_interval_features.clear()
    engine.last_verdicts.clear()
    identities.token_by_solana_pool.clear()
    identities.token_by_4663_contract.clear()
    identities.pons_pool_tokens.clear()
    identities.creator_by_token.clear()
    fact_rows = conn.execute(
        "SELECT ce.venue_id,vf.facts_json FROM op_canonical_events ce "
        "JOIN op_venue_event_facts vf USING(canonical_event_id) "
        "WHERE ce.confirmation != 'orphaned' AND ce.event_kind != 'correction' "
        "AND NOT EXISTS (SELECT 1 FROM op_canonical_events correction "
        "WHERE correction.correction_of=ce.canonical_event_id AND correction.confirmation='orphaned') "
        "ORDER BY ce.block_or_slot,"
        "COALESCE(ce.transaction_index,-1),ce.event_index,ce.inserted_at"
    ).fetchall()
    for row in fact_rows:
        if not is_active_capture_venue(row["venue_id"]):
            continue
        decoded = _decoded_from_json(json.loads(row["facts_json"]))
        identities.learn(VenueId(row["venue_id"]), decoded)

    state_rows = conn.execute(
        "SELECT s.*,ce.confirmation,ce.block_or_slot,ce.transaction_index,ce.event_index "
        "FROM op_state_snapshots s JOIN op_canonical_events ce "
        "ON ce.canonical_event_id=s.observed_event_id "
        "WHERE ce.confirmation!='orphaned' AND NOT EXISTS ("
        "SELECT 1 FROM op_canonical_events correction "
        "WHERE correction.correction_of=ce.canonical_event_id "
        "AND correction.confirmation='orphaned') "
        "ORDER BY ce.block_or_slot,COALESCE(ce.transaction_index,-1),"
        "ce.event_index,s.observed_at,s.inserted_at"
    ).fetchall()
    latest_rows = {}
    states_by_event = {}
    for row in state_rows:
        state = _state_from_json(row["state_json"])
        if not is_active_capture_venue(state.venue_id):
            continue
        states_by_event[(row["launch_id"], row["observed_event_id"])] = state
        latest_rows[row["launch_id"]] = row
        engine.states[row["launch_id"]] = state
    for launch_id, row in latest_rows.items():
        state = engine.states[launch_id]
        fingerprint = content_fingerprint(
            {
                "state": state,
                "observed_event_id": row["observed_event_id"],
                "observed_at": row["observed_at"],
            }
        )
        engine.last_observations[launch_id] = LaunchSnapshot(
            launch_id=launch_id,
            observed_event_id=row["observed_event_id"],
            observed_at=row["observed_at"],
            age_ms=max(0, int((_parse_iso(state.observed_at) - _parse_iso(state.launched_at)).total_seconds() * 1000)),
            progress_bps=state.progress_bps or 0,
            confirmation=ConfirmationState(row["confirmation"]),
            state_fingerprint=fingerprint,
            source_gap_open=bool(state.blocker_codes and "EVENT_GAP" in state.blocker_codes),
            ineligible_reason=state.blocker_codes[0] if state.blocker_codes else None,
        )
        prediction = conn.execute(
            "SELECT p.* FROM op_predictions p JOIN op_state_snapshots s "
            "ON s.snapshot_id=p.snapshot_id WHERE s.launch_id=? "
            "ORDER BY p.observed_at DESC,p.inserted_at DESC LIMIT 1",
            (launch_id,),
        ).fetchone()
        if prediction is not None:
            engine.last_verdicts[launch_id] = DirectionalVerdict(
                RadarZone(prediction["zone"]),
                bool(prediction["would_fire"]),
                tuple(json.loads(prediction["reason_codes_json"])),
                prediction["rule_version"],
                prediction["truth_label"],
            )

    landmark_rows = conn.execute(
        "SELECT lm.launch_id,lm.observed_event_id,lm.observed_at FROM op_landmark_states lm "
        "WHERE lm.status IN ('tentative','canonical') AND NOT EXISTS ("
        "SELECT 1 FROM op_landmark_states successor "
        "WHERE successor.supersedes_landmark_id=lm.landmark_id AND successor.status='orphaned') "
        "ORDER BY lm.observed_at,lm.inserted_at"
    ).fetchall()
    landmark_history: dict[str, list[LaunchState]] = {}
    for row in landmark_rows:
        state = states_by_event.get((row["launch_id"], row["observed_event_id"]))
        if state is not None:
            landmark_history.setdefault(row["launch_id"], []).append(state)
    for launch_id, history in landmark_history.items():
        engine.last_landmark_states[launch_id] = history[-1]
        if len(history) >= 2:
            prior_features = (
                point_in_time_features(history[-2], prior=history[-3])
                if len(history) >= 3
                else None
            )
            if prior_features is not None:
                engine.prior_interval_features[launch_id] = prior_features
            engine.last_interval_features[launch_id] = add_feature_derivatives(
                point_in_time_features(history[-1], prior=history[-2]),
                prior=prior_features,
            )


def load_pending_confirmations(conn) -> dict[str, set[str]]:
    active = tuple(sorted(venue.value for venue in active_capture_venues()))
    placeholders = ",".join("?" for _ in active)
    rows = conn.execute(
        "SELECT canonical_event_id,transaction_id FROM op_canonical_events ce "
        "WHERE confirmation='tentative' AND event_kind!='correction' "
        f"AND venue_id IN ({placeholders}) AND NOT EXISTS ("
        "SELECT 1 FROM op_canonical_events correction "
        "WHERE correction.correction_of=ce.canonical_event_id "
        "AND correction.confirmation IN ('finalized','orphaned'))",
        active,
    ).fetchall()
    pending: dict[str, set[str]] = {}
    for row in rows:
        pending.setdefault(row["transaction_id"], set()).add(row["canonical_event_id"])
    return pending


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cursor_signature(cursor: str | None) -> str | None:
    if not cursor:
        return None
    parts = cursor.split(":", 2)
    return parts[1] if len(parts) == 3 else None


def _newest_solana_cursor(
    live_cursor: str | None,
    recovery_cursor: str | None,
) -> str | None:
    """Keep a live tail when replay visits an older slot."""
    live_slot = _cursor_block(live_cursor)
    recovery_slot = _cursor_block(recovery_cursor)
    if live_cursor is not None and (
        recovery_cursor is None
        or recovery_slot is None
        or (live_slot is not None and live_slot > recovery_slot)
    ):
        return live_cursor
    return recovery_cursor


def _finish_recovery(
    conn,
    runtime,
    source_key: str,
    *,
    observed_at: str,
    newest_observed_cursor: str | None = None,
) -> None:
    current = load_frontier(conn, source_key)
    if current is None:
        return
    recoverable_gap_ids = tuple(
        row["gap_id"]
        for row in list_open_gaps(conn, source_key)
        if row["gap_kind"] != GapKind.DECODE_FAILURE.value
    )
    if recoverable_gap_ids:
        target_cursor = newest_observed_cursor or current.latest_observed_cursor
        current = apply_frontier_batch(
            conn,
            FrontierBatch(
                source_key=source_key,
                expected_version=current.version,
                previous_durable_cursor=current.durable_cursor,
                newest_observed_cursor=target_cursor,
                contiguous_durable_cursor=target_cursor,
                resume_cursor=None,
                reached_previous_frontier=True,
                enumeration_complete=True,
                decode_complete=True,
                observed_at=observed_at,
                resolved_gap_ids=recoverable_gap_ids,
            ),
        )
    if not current.has_open_gap:
        runtime.mark_recovered(source_key)


def _stored_detail(conn, source_key: str) -> dict:
    """The detail already persisted for a source, or an empty mapping.

    A heartbeat replaces `detail_json` wholesale, so any field that must outlive one
    poll has to be read back and carried forward explicitly.
    """
    try:
        row = conn.execute(
            "SELECT detail_json FROM op_runtime_status WHERE source_key=?",
            (source_key,),
        ).fetchone()
    except Exception:
        return {}
    if row is None:
        return {}
    try:
        stored = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def _report_lane_failure(runtime, source_key: str, detail: str) -> None:
    """Report a lane's transport failure without letting the report kill the lane.

    Every capture lane calls this from inside its own `except` handler, which is the
    one place that cannot afford to raise: an exception there escapes the reconnect
    loop and ends the coroutine, and because the Task reference is held in `tasks`
    until shutdown, Python never logs "exception was never retrieved". On 2026-08-04
    `record_source_failure` raised `ImmutableConflict` for a quiet Bags frontier and
    all four shards vanished for 17h behind a runtime row frozen at `healthy`.

    The two writes are independent on purpose. A failed frontier append must not stop
    the board from learning the source is degraded, which is the only signal an
    operator sees.
    """
    for step, write in (
        (
            "frontier",
            lambda: record_source_failure(
                runtime.conn,
                source_key=source_key,
                observed_at=real_utc_now_iso(),
                detail=detail,
            ),
        ),
        (
            "heartbeat",
            lambda: runtime.heartbeat(
                source_key, status="degraded", blocker_code="SOURCE_FAILURE"
            ),
        ),
    ):
        try:
            write()
        except Exception as exc:
            print(
                f"lane failure report failed: source={source_key} step={step} "
                f"{type(exc).__name__}: {str(exc)[:200] or '(no message)'} "
                f"original={detail[:200]}",
                file=sys.stderr,
                flush=True,
            )


async def _supervise_lane(factory, *, label: str, stop, backoff_s: float = 1.0) -> None:
    """Keep a capture lane running; a lane that ends is a silent capture blackout.

    Deliberately touches no database. The failure this backstops is a lane dying
    inside its own error-reporting path, so a supervisor that reported through the
    same ledger could die the same way and restore nothing.

    Returning without `stop` counts as a crash. A lane's contract is to run until
    shutdown, and a quiet `return` produced exactly the same invisible outage as a
    raise.
    """
    while not stop.is_set():
        try:
            await factory()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - nothing below may end quietly
            reason = f"{type(exc).__name__}: {str(exc)[:300] or '(no message)'}"
        else:
            reason = "returned without stop being set"
        if stop.is_set():
            return
        print(
            f"capture lane restarting: lane={label} reason={reason}",
            file=sys.stderr,
            flush=True,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff_s)
        except asyncio.TimeoutError:
            pass


def _event_expectation_detail(runtime, source_key: str, detail: dict) -> dict:
    """Attach the event-age bound so a subscribed-but-silent lane grades degraded.

    A live socket that heartbeats on a timer says nothing about delivery. On
    2026-08-07 `wss://solana-rpc.publicnode.com` accepted the pump.fun
    `logsSubscribe`, returned a subscription id, and then delivered zero events.
    Reproduced outside the worker: publicnode 0 events in 20s against 5 on
    `api.mainnet-beta.solana.com`. The lane reported `healthy` for 20 minutes with
    an empty stream, because it declared no expectation.
    """
    if not detail.get("last_chain_event_at"):
        row = (
            runtime.conn.execute(
                "SELECT MAX(chain_timestamp) FROM op_raw_events WHERE source_key=?",
                (source_key,),
            ).fetchone()
            if hasattr(runtime, "conn") and hasattr(runtime.conn, "execute")
            else None
        )
        if row is not None and row[0] is not None:
            detail["last_chain_event_at"] = row[0]
    detail["expected_event_max_age_s"] = _EXPECTED_EVENT_MAX_AGE_S
    detail.setdefault(
        "event_expectation_started_at",
        runtime.now_fn() if hasattr(runtime, "now_fn") else real_utc_now_iso(),
    )
    return detail


def _source_heartbeat(runtime, source_key: str) -> None:
    """Refresh transport liveness without erasing a durable integrity blocker."""
    if not runtime.source_gapped(source_key):
        if source_key == "bags_solana:ws":
            # Bags declares no event expectation on purpose. Its launches are
            # genuinely low cadence -- sampled 2026-08-03, the feed's newest pools
            # last traded between 3 and 17 hours ago and none had traded within the
            # hour -- so the previous 900s bound was guaranteed to trip and reported
            # a working transport as a dead one. Per the Pons readiness design, a
            # quiet Bags source shows event age as telemetry and only Pons declares
            # an expectation, "because its launch flow is high cadence". Transport
            # liveness is still bounded by `transport_stale_after_s`.
            runtime.heartbeat(
                source_key,
                detail={"transport_stale_after_s": _SOLANA_TRANSPORT_STALE_AFTER_S},
            )
        elif source_key == "pumpfun_solana:ws":
            # pump.fun is the HIGHEST cadence source the radar has -- 7,890 raw events
            # in the first 20 minutes after reconnect. It was grouped with quiet Bags
            # and inherited "declares no expectation", which is the reason a dead
            # subscription read as healthy. Five minutes of total silence on the
            # pump.fun program does not happen.
            runtime.heartbeat(
                source_key,
                detail=_event_expectation_detail(
                    runtime,
                    source_key,
                    {"transport_stale_after_s": _SOLANA_TRANSPORT_STALE_AFTER_S},
                ),
            )
        elif source_key in {"bags_robinhood_4663:ws", "pons_v1_4663:ws"}:
            row = (
                runtime.conn.execute(
                    "SELECT detail_json FROM op_runtime_status WHERE source_key=?",
                    (source_key,),
                ).fetchone()
                if hasattr(runtime, "conn") and hasattr(runtime.conn, "execute")
                else None
            )
            try:
                detail = json.loads(row[0]) if row is not None else {}
            except (TypeError, ValueError):
                detail = {}
            detail["transport_stale_after_s"] = _EVM_TRANSPORT_STALE_AFTER_S
            if source_key == "pons_v1_4663:ws":
                detail = _event_expectation_detail(runtime, source_key, detail)
            runtime.heartbeat(source_key, detail=detail)
        else:
            runtime.heartbeat(source_key)
        return
    row = runtime.conn.execute(
        "SELECT blocker_code FROM op_runtime_status WHERE source_key=?", (source_key,)
    ).fetchone()
    blocker = row[0] if row is not None and row[0] else "EVENT_GAP"
    runtime.heartbeat(source_key, status="degraded", blocker_code=blocker)


def _drain_solana_quote_completions(
    probe,
    engine,
    *,
    observed_at: str,
) -> int:
    completions = probe.drain_completions(limit=32)
    for result in completions:
        engine.observe_remote_completion(result, observed_at=observed_at)
    return len(completions)


def _drain_evm_quote_completions(probe, engine, *, observed_at: str) -> int:
    completions = probe.drain_completions(limit=32)
    for result in completions:
        engine.observe_remote_completion(result, observed_at=observed_at)
    return len(completions)


def _paper_probe_quote_fn(probe, engine):
    """Adapt the shared non-blocking Solana probe to one Radar fill request."""
    def quote(request):
        state = engine.states.get(str(request["launch_id"]))
        if (
            state is None
            or state.venue_id is not VenueId.PUMPFUN_SOLANA
            or state.token_id != str(request["token_id"])
            or not state.quote_mint
        ):
            raise QuoteRejected("identity_invalid")
        market = engine._market_input(state)
        if market is None:
            raise QuoteRejected("quote_unavailable")
        usd_price_micros, quote_decimals, _fee_bps, _network_cost = market
        amount_atomic = quote_atomic_for_usd(
            int(request["clip_usd_micros"]), usd_price_micros, quote_decimals
        )
        result = probe.paper_quote(
            request_id=str(request["request_id"]),
            input_mint=state.quote_mint,
            output_mint=state.token_id,
            amount_atomic=amount_atomic,
            input_decimals=quote_decimals,
            output_decimals=6,
        )
        if result.get("status") == "pending":
            raise QuoteDeferred
        if result.get("status") == "rejected":
            raise QuoteRejected(str(result.get("reason") or "quote_unavailable"))
        economics = result.get("economics")
        if result.get("status") != "filled" or not isinstance(economics, dict):
            raise QuoteRejected("malformed_quote")
        return economics

    return quote


def _paper_quote_fn(solana_probe, evm_probe, engine):
    """Dispatch one entry quote to the venue-specific background probe."""
    solana_quote = _paper_probe_quote_fn(solana_probe, engine) if solana_probe else None

    def quote(request):
        state = engine.states.get(str(request["launch_id"]))
        if state is None or state.token_id != str(request["token_id"]):
            raise QuoteRejected("identity_invalid")
        if state.venue_id is VenueId.PUMPFUN_SOLANA:
            if solana_quote is None:
                raise QuoteRejected("quote_unavailable")
            return solana_quote(request)
        if state.venue_id not in {
            VenueId.BAGS_ROBINHOOD_4663,
            VenueId.PONS_V1_ROBINHOOD_4663,
        } or evm_probe is None:
            raise QuoteRejected("identity_invalid")
        market = engine._market_input(state)
        if market is None:
            raise QuoteRejected("quote_unavailable")
        usd_price_micros, quote_decimals, _fee_bps, _network_cost = market
        amount_atomic = quote_atomic_for_usd(
            int(request["clip_usd_micros"]), usd_price_micros, quote_decimals
        )
        result = evm_probe.paper_entry(
            request_id=str(request["request_id"]),
            state=state,
            quote_in_atomic=amount_atomic,
            state_fingerprint=str(request["state_fingerprint"]),
        )
        if result.get("status") == "pending":
            raise QuoteDeferred
        if result.get("status") == "rejected":
            raise QuoteRejected(str(result.get("reason") or "quote_unavailable"))
        economics = result.get("economics")
        if result.get("status") != "filled" or not isinstance(economics, dict):
            raise QuoteRejected("malformed_quote")
        return economics

    return quote


def _paper_exit_quote_fn(probe, *, sampler=None):
    """Adapt the shared Solana probe to one Radar exact-quantity sell quote."""
    def quote(request):
        quote_asset = str(request["quote_asset"])
        if quote_asset == NATIVE_SOL_MINT:
            from sniper.research.memecoin_opportunity.state import PUMP_SOL_QUOTE_MINT

            quote_asset = PUMP_SOL_QUOTE_MINT
        observation_id = str(request["observation_id"])
        result = probe.paper_exit_quote(
            request_id=observation_id,
            input_mint=str(request["token_id"]),
            output_mint=quote_asset,
            amount_atomic=int(request["quantity_atomic"]),
            input_decimals=int(request["token_decimals"]),
            output_decimals=int(request["quote_decimals"]),
        )
        if result.get("status") == "pending":
            if (
                sampler is not None
                and "scheduled_age_ms" in request
                and probe.paper_request_pending(observation_id)
            ):
                sampler.track(
                    request,
                    requested_at=str(
                        request.get("now_iso") or real_utc_now_iso()
                    ),
                )
            raise QuoteDeferred
        if result.get("status") == "rejected":
            raise QuoteRejected(str(result.get("reason") or "quote_unavailable"))
        economics = result.get("economics")
        if result.get("status") != "filled" or not isinstance(economics, dict):
            raise QuoteRejected("malformed_quote")
        return economics

    return quote


def _paper_exit_quote_dispatch_fn(solana_probe, evm_probe, *, sampler=None):
    """Dispatch one exact-quantity exit quote by venue identity."""
    solana_quote = (
        _paper_exit_quote_fn(solana_probe, sampler=sampler)
        if solana_probe
        else None
    )

    def quote(request):
        venue = VenueId(str(request["venue_id"]))
        if venue is VenueId.PUMPFUN_SOLANA:
            if solana_quote is None:
                raise QuoteRejected("quote_unavailable")
            return solana_quote(request)
        if venue not in {
            VenueId.BAGS_ROBINHOOD_4663,
            VenueId.PONS_V1_ROBINHOOD_4663,
        } or evm_probe is None:
            raise QuoteRejected("identity_invalid")
        observation_id = str(request["observation_id"])
        result = evm_probe.paper_exit(
            request_id=observation_id,
            venue_id=venue,
            token_id=str(request["token_id"]),
            quantity_atomic=int(request["quantity_atomic"]),
            token_decimals=int(request["token_decimals"]),
            quote_decimals=int(request["quote_decimals"]),
            route_binding=dict(request["route_binding"]),
        )
        if result.get("status") == "pending":
            if (
                sampler is not None
                and "scheduled_age_ms" in request
                and evm_probe.paper_request_pending(observation_id)
            ):
                sampler.track(
                    request,
                    requested_at=str(
                        request.get("now_iso") or real_utc_now_iso()
                    ),
                )
            raise QuoteDeferred
        if result.get("status") == "rejected":
            raise QuoteRejected(str(result.get("reason") or "quote_unavailable"))
        economics = result.get("economics")
        if result.get("status") != "filled" or not isinstance(economics, dict):
            raise QuoteRejected("malformed_quote")
        return economics

    return quote


def _drain_paper_quote_completions(
    conn,
    *,
    sampler: PaperSampler,
    solana_probe,
    evm_probe,
) -> dict[str, int]:
    """Store completed paper quotes without a new provider request."""
    counters = {"completed": 0, "stored": 0, "orphaned": 0, "deferred": 0}

    def store(bound) -> None:
        try:
            stored = append_bound_paper_completion(
                conn,
                request=bound.request,
                completion=bound.completion,
            )
        except sqlite3.Error:
            sampler.retry(bound)
            counters["deferred"] += 1
            return
        if stored:
            counters["stored"] += 1

    for bound in sampler.take_retries():
        store(bound)
    for probe in (solana_probe, evm_probe):
        if probe is None:
            continue
        for completion in probe.drain_paper_completions(limit=32):
            counters["completed"] += 1
            bound = sampler.consume(completion)
            if bound is None:
                counters["orphaned"] += 1
                continue
            store(bound)
    return counters


def _report_paper_sampler_failure(
    conn,
    *,
    now_iso: str,
    active_count: int,
    error_type: str,
) -> None:
    """Report a sampler failure when the status lane is available."""
    try:
        upsert_runtime_status(
            conn,
            source_key=PAPER_SAMPLER_SOURCE_KEY,
            status="degraded",
            blocker_code="PAPER_SAMPLER_FAILURE",
            heartbeat_at=now_iso,
            queue_depth=active_count,
            detail={"error_type": error_type},
        )
    except Exception:
        return


def _drive_paper_adapter_safely(
    conn,
    *,
    quote_fn,
    now_iso: str,
) -> dict[str, int]:
    """The optional measurement lane may degrade; it may never stop capture."""
    try:
        counters = drive_paper_adapter(
            conn,
            quote_fn=quote_fn,
            now_iso=now_iso,
        )
    except Exception as exc:
        upsert_runtime_status(
            conn,
            source_key="opportunity:paper_adapter",
            status="degraded",
            blocker_code="PAPER_ADAPTER_FAILURE",
            heartbeat_at=now_iso,
            queue_depth=0,
            detail={"error_type": type(exc).__name__},
        )
        return {"adapter_error": 1}
    upsert_runtime_status(
        conn,
        source_key="opportunity:paper_adapter",
        status="healthy",
        blocker_code=None,
        heartbeat_at=now_iso,
        queue_depth=0,
        detail=counters,
    )
    return counters


def _drive_paper_lifecycle_safely(
    conn,
    *,
    quote_fn,
    now_iso: str,
) -> dict[str, int]:
    """The Radar exit measurement can degrade, but capture must continue."""
    try:
        counters = drive_paper_lifecycle(conn, quote_fn=quote_fn, now_iso=now_iso)
    except Exception as exc:
        upsert_runtime_status(
            conn,
            source_key="opportunity:paper_lifecycle",
            status="degraded",
            blocker_code="PAPER_LIFECYCLE_FAILURE",
            heartbeat_at=now_iso,
            queue_depth=0,
            detail={"error_type": type(exc).__name__},
        )
        return {"lifecycle_error": 1}
    upsert_runtime_status(
        conn,
        source_key="opportunity:paper_lifecycle",
        status="healthy",
        blocker_code=None,
        heartbeat_at=now_iso,
        queue_depth=int(counters.get("eligible", 0)),
        detail=counters,
    )
    return counters


def _drive_adaptive_lifecycle_safely(
    conn,
    *,
    quote_fn,
    observation_fn,
    now_iso: str,
) -> dict[str, int]:
    """A v3 paper failure must not stop Radar capture."""
    try:
        counters = drive_adaptive_lifecycle(
            conn,
            quote_fn=quote_fn,
            observation_fn=observation_fn,
            now_iso=now_iso,
        )
    except Exception as exc:
        upsert_runtime_status(
            conn,
            source_key="opportunity:adaptive_paper_lifecycle",
            status="degraded",
            blocker_code="ADAPTIVE_PAPER_LIFECYCLE_FAILURE",
            heartbeat_at=now_iso,
            queue_depth=0,
            detail={"error_type": type(exc).__name__},
        )
        return {"lifecycle_error": 1}
    upsert_runtime_status(
        conn,
        source_key="opportunity:adaptive_paper_lifecycle",
        status="healthy",
        blocker_code=None,
        heartbeat_at=now_iso,
        queue_depth=int(counters.get("eligible", 0)),
        detail=counters,
    )
    return counters


def _activate_paper_lifecycles(
    conn,
    *,
    adaptive_v3_enabled: bool,
    now_iso: str,
    realistic_v3_authorised: bool = False,
) -> dict[str, str]:
    """Activate v2, activate adaptive v3 on its setting, activate realistic v3 if armed.

    `radar-realistic-v3` refuses to open its cohort until the paper sampler measures the
    maximum baseline age. That refusal is the design, not a fault, so it must not stop
    the worker or the other lifecycles.
    """
    versions = {
        "v2": activate_paper_lifecycle(conn, now_iso=now_iso),
    }
    if adaptive_v3_enabled:
        versions["v3"] = activate_adaptive_lifecycle(conn, now_iso=now_iso)
    try:
        versions["realistic_v3"] = activate_realistic_v3(
            conn,
            now_iso=now_iso,
            authorised=realistic_v3_authorised,
        )
    except BaselineAgeNotPreregistered:
        # The measured integer is not preregistered yet.
        pass
    except ActivationNotAuthorised:
        # The integer ships, the cohort stays CLOSED. Opening it is a separate operator
        # action: `MEMECOIN_RADAR_REALISTIC_V3_ACTIVATE=1`. Once the row exists the
        # cohort keeps running even if the control goes back to off.
        pass
    return versions


def _drive_realistic_v3_safely(conn, *, now_iso: str) -> dict[str, int]:
    """A realistic-v3 failure must not stop Radar capture or the v2 measurement."""
    try:
        counters = drive_realistic_v3(conn, now_iso=now_iso)
    except Exception as exc:
        upsert_runtime_status(
            conn,
            source_key="opportunity:realistic_v3_paper_lifecycle",
            status="degraded",
            blocker_code="REALISTIC_V3_LIFECYCLE_FAILURE",
            heartbeat_at=now_iso,
            queue_depth=0,
            detail={"error_type": type(exc).__name__},
        )
        return {"lifecycle_error": 1}
    upsert_runtime_status(
        conn,
        source_key="opportunity:realistic_v3_paper_lifecycle",
        status="healthy",
        blocker_code=None,
        heartbeat_at=now_iso,
        queue_depth=int(counters.get("eligible", 0)),
        detail=counters,
    )
    return counters


def _drive_paper_lifecycles_safely(
    conn,
    *,
    quote_fn,
    observation_fn,
    now_iso: str,
    adaptive_v3_enabled: bool = False,
    sampler: PaperSampler | None = None,
    solana_probe=None,
    evm_probe=None,
) -> dict[str, dict[str, int]]:
    """Drive v2 and drive v3 only when its setting is true."""
    sampler_failed = False
    if sampler is not None:
        try:
            _drain_paper_quote_completions(
                conn,
                sampler=sampler,
                solana_probe=solana_probe,
                evm_probe=evm_probe,
            )
        except Exception as exc:
            sampler_failed = True
            _report_paper_sampler_failure(
                conn,
                now_iso=now_iso,
                active_count=sampler.active_count,
                error_type=type(exc).__name__,
            )
    v2 = _drive_paper_lifecycle_safely(
        conn, quote_fn=quote_fn, now_iso=now_iso,
    )
    if (
        sampler is not None
        and not sampler_failed
        and sampler.persistence_due(now_iso=now_iso)
    ):
        try:
            sampler.maybe_persist(
                conn,
                now_iso=now_iso,
                probe_metrics={
                    "solana": solana_probe.metrics() if solana_probe else {},
                    "evm": evm_probe.metrics() if evm_probe else {},
                },
            )
        except Exception as exc:
            _report_paper_sampler_failure(
                conn,
                now_iso=now_iso,
                active_count=sampler.active_count,
                error_type=type(exc).__name__,
            )
    v3 = {"disabled": 1}
    if adaptive_v3_enabled:
        v3 = _drive_adaptive_lifecycle_safely(
            conn,
            quote_fn=quote_fn,
            observation_fn=observation_fn,
            now_iso=now_iso,
        )
    # Scores the v2 observation series against the on-time first-quote baseline. It
    # requests nothing, so it runs whenever its activation row exists.
    realistic_v3 = _drive_realistic_v3_safely(conn, now_iso=now_iso)
    return {"v2": v2, "v3": v3, "realistic_v3": realistic_v3}


def _adaptive_observation_facts(
    engine: OpportunityEngine,
    entry,
    *,
    now_iso: str,
) -> dict[str, object]:
    """Build fail-closed v3 facts from the current and entry snapshots."""
    del now_iso
    launch_id = str(entry["launch_id"])
    current = engine.states.get(launch_id)
    entry_state = engine.state_by_fingerprint.get(str(entry["state_fingerprint"]))
    point = engine.last_observations.get(launch_id)
    if entry_state is None and getattr(engine, "conn", None) is not None:
        row = engine.conn.execute(
            "SELECT s.state_json,s.observed_event_id,s.observed_at "
            "FROM op_forward_alerts a JOIN op_state_snapshots s "
            "ON s.snapshot_id=a.snapshot_id WHERE a.alert_id=?",
            (entry.get("alert_id"),),
        ).fetchone()
        if row is not None:
            candidate = launch_state_from_json(row["state_json"])
            candidate_fingerprint = content_fingerprint({
                "state": candidate,
                "observed_event_id": row["observed_event_id"],
                "observed_at": row["observed_at"],
            })
            if candidate_fingerprint == str(entry["state_fingerprint"]):
                entry_state = candidate
    if current is None or entry_state is None or point is None:
        return {
            "canonical_event_id": None,
            "creator_sale_since_entry": None,
            "tape_complete": False,
            "depth_pull_flag": None,
            "liquidity_removed_fraction": None,
            "depth_provenance": "missing",
            "capture_version": "missing",
            "route_degraded": None,
            "is_break": None,
            "net": None,
            "chain_vol_p90": None,
        }

    removed_bps = liquidity_removed_fraction_bps(current, entry_state)
    pool_changed = (
        entry_state.pool_id is not None
        and current.pool_id is not None
        and entry_state.pool_id != current.pool_id
    )
    route_blocked = any(
        "ROUTE" in code or "POOL" in code for code in current.blocker_codes
    )
    buy_delta = max(0, current.buy_count - entry_state.buy_count)
    sell_delta = max(0, current.sell_count - entry_state.sell_count)
    flow_count = buy_delta + sell_delta
    return {
        "canonical_event_id": point.observed_event_id,
        "creator_sale_since_entry": (
            current.creator_sale_count > entry_state.creator_sale_count
        ),
        "tape_complete": True,
        "depth_pull_flag": None if removed_bps is None else removed_bps > 0,
        "liquidity_removed_fraction": (
            None if removed_bps is None else removed_bps / 10_000
        ),
        "depth_provenance": "trusted" if removed_bps is not None else "missing",
        "capture_version": (
            "v6-direct-v1" if removed_bps is not None else "missing"
        ),
        "route_degraded": bool(pool_changed or route_blocked),
        "is_break": None,
        "arrival_rate": None,
        "buy_share": None if flow_count == 0 else buy_delta / flow_count,
        "realized_vol": None,
        "prior_low": None,
        "net": (
            current.independent_capital_in_atomic
            - current.independent_capital_out_atomic
            - entry_state.independent_capital_in_atomic
            + entry_state.independent_capital_out_atomic
        ),
        "chain_vol_p90": None,
    }


def _engine_state_available(engine, decoded: DecodedVenueEvent, launch_id: str) -> bool:
    return decoded.event_kind is EventKind.LAUNCH or launch_id in engine.states


def _solana_recovery_cursors(frontier, open_gaps) -> tuple[str | None, str | None]:
    """Freeze recovery on the gap's original frontier while live truth advances."""
    previous_signature = _cursor_signature(frontier.durable_cursor)
    resume_before = None
    if frontier.has_open_gap:
        enumeration_gaps = [
            row for row in open_gaps
            if row["gap_kind"] == GapKind.ENUMERATION_BOUND.value
        ]
        if enumeration_gaps:
            gap = enumeration_gaps[-1]
            previous_signature = _cursor_signature(gap["previous_durable_cursor"])
            resume_before = gap["resume_cursor"]
    return previous_signature, resume_before


@dataclass(frozen=True)
class IndexedSolanaRecoveryResult:
    transactions: tuple[dict[str, object], ...]
    complete: bool
    resume_cursor: str | None


def _indexed_solana_recovery(
    fetch_page,
    *,
    previous_durable_signature: str | None,
    max_pages: int,
    start_cursor: str | None = None,
    not_before_timestamp_s: int | None = None,
) -> IndexedSolanaRecoveryResult:
    """Fetch full indexed transaction pages while preserving the frozen frontier."""
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    token = (
        start_cursor.removeprefix("indexed:")
        if start_cursor and start_cursor.startswith("indexed:")
        else None
    )
    transactions: list[dict[str, object]] = []
    for _ in range(max_pages):
        result = fetch_page(token) or {}
        rows = list(result.get("data") or [])
        for row in rows:
            signatures = ((row.get("transaction") or {}).get("signatures") or [])
            signature = str(signatures[0]) if signatures else ""
            if signature and signature == previous_durable_signature:
                return IndexedSolanaRecoveryResult(tuple(transactions), True, None)
            block_time = row.get("blockTime")
            if (
                previous_durable_signature is None
                and not_before_timestamp_s is not None
                and isinstance(block_time, (int, float))
                and block_time < not_before_timestamp_s
            ):
                return IndexedSolanaRecoveryResult(tuple(transactions), True, None)
            if signature:
                transactions.append(row)
        next_token = result.get("paginationToken")
        if not rows or not next_token:
            return IndexedSolanaRecoveryResult(
                tuple(transactions), previous_durable_signature is None, None
            )
        token = str(next_token)
    return IndexedSolanaRecoveryResult(
        tuple(transactions), False, f"indexed:{token}" if token else "indexed:bounded"
    )


def _retry_stored_solana_decode_gaps(
    conn,
    runtime,
    *,
    source_key: str,
    venue: VenueId,
    program_id: str,
    bags_pool_mints: dict[str, str] | None = None,
) -> int:
    """Retry immutable raw rows after decoder or identity support improves."""
    retried = 0
    for gap in list_open_gaps(conn, source_key):
        if gap["gap_kind"] != GapKind.DECODE_FAILURE.value:
            continue
        row = conn.execute(
            "SELECT * FROM op_raw_events WHERE source_key=? AND source_cursor=?",
            (source_key, gap["resume_cursor"]),
        ).fetchone()
        exact_cursor = row is not None
        if row is None and venue is VenueId.BAGS_SOLANA and bags_pool_mints:
            row = conn.execute(
                "SELECT * FROM op_raw_events WHERE source_key=? AND source_cursor=?",
                (source_key, gap["newest_observed_cursor"]),
            ).fetchone()
        if row is None:
            continue
        payload = json.loads(row["raw_payload_json"])
        if (
            venue is VenueId.BAGS_SOLANA
            and bags_pool_mints
            and row["source_provider"] == "solana_rpc_recovery"
            and row["token_id"] == BAGS_DBC_PROGRAM
            and gap["detail"] in {
                DecodeBlocker.MISSING_IDENTITY.value,
                DecodeBlocker.UNKNOWN_VERSION.value,
            }
        ):
            resolve_gap(
                conn,
                gap["gap_id"],
                resolved_at=real_utc_now_iso(),
            )
            retried += 1
            continue
        if not exact_cursor:
            continue
        envelope = OpportunitySourceEnvelope(
            existing_raw_event_id=row["raw_event_id"],
            source_key=row["source_key"],
            source_provider=row["source_provider"],
            source_cursor=row["source_cursor"],
            venue_id=venue,
            program_or_contract_id=program_id,
            transaction_id=row["transaction_id"],
            block_or_slot=row["block_or_slot"],
            transaction_index=row["transaction_index"],
            event_index=row["event_index"],
            block_hash=row["block_hash"],
            chain_timestamp=row["chain_timestamp"],
            received_at=row["received_at"],
            confirmation=ConfirmationState(row["confirmation"]),
            payload=payload,
            token_hint=row["token_id"],
        )
        runtime.process_fn(envelope)
        retried += 1
    return retried


async def _recover_solana_frontier(
    conn,
    runtime,
    *,
    venue: VenueId,
    program_id: str,
    rpc_url: str,
    max_pages: int,
    indexed_recovery: bool = False,
    rpc_governor=None,
    bags_pool_mints: dict[str, str] | None = None,
) -> bool:
    """Bounded newest-first recovery. A page bound remains a durable data gap."""
    source_key = f"{venue.value}:ws"
    frontier = load_frontier(conn, source_key)
    if frontier is None:
        return True
    _retry_stored_solana_decode_gaps(
        conn,
        runtime,
        source_key=source_key,
        venue=venue,
        program_id=program_id,
        bags_pool_mints=bags_pool_mints,
    )
    frontier = load_frontier(conn, source_key)
    live_latest_cursor = frontier.latest_observed_cursor
    open_gaps = list_open_gaps(conn, source_key)
    previous_signature, resume_before = _solana_recovery_cursors(
        frontier, open_gaps
    )
    not_before_timestamp_s = None
    if previous_signature is None and open_gaps:
        not_before_timestamp_s = int(
            min(_parse_iso(row["opened_at"]) for row in open_gaps).timestamp()
        )

    def fetch_page(before):
        # A reconnect has an exact durable signature, so one larger index page
        # can bridge an ordinary Pump burst without another HTTP call. Bootstrap
        # recovery has no anchor and keeps the smaller transaction-read budget.
        config = {
            "commitment": "confirmed",
            "limit": 100 if previous_signature else 10,
        }
        if before:
            config["before"] = before
        rows = json_rpc_http(rpc_url, "getSignaturesForAddress", [program_id, config]) or []
        return rows, (rows[-1].get("signature") if rows else None)

    try:
        if indexed_recovery:
            def fetch_indexed_page(token):
                config = {
                    "transactionDetails": "full",
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 1,
                    "sortOrder": "desc",
                    "limit": 100,
                    "commitment": "confirmed",
                }
                if token:
                    config["paginationToken"] = token
                return json_rpc_http(
                    rpc_url, "getTransactionsForAddress", [program_id, config]
                )

            recovery_call = lambda: _indexed_solana_recovery(
                fetch_indexed_page,
                previous_durable_signature=previous_signature,
                max_pages=max_pages,
                start_cursor=resume_before,
                not_before_timestamp_s=not_before_timestamp_s,
            )
            recovered = (
                await rpc_governor.call(RpcPriority.RECOVERY, recovery_call)
                if rpc_governor else await asyncio.to_thread(recovery_call)
            )
            transactions = list(reversed(recovered.transactions))
            signatures = [
                str((((tx.get("transaction") or {}).get("signatures") or [""])[0]))
                if isinstance(tx, dict)
                else ""
                for tx in transactions
            ]
        else:
            recovery_call = lambda: bounded_solana_recovery(
                fetch_page,
                previous_durable_signature=previous_signature,
                max_pages=max_pages,
                start_before=resume_before,
                not_before_timestamp_s=not_before_timestamp_s,
            )
            recovered = (
                await rpc_governor.call(RpcPriority.RECOVERY, recovery_call)
                if rpc_governor else await asyncio.to_thread(recovery_call)
            )
            signatures = list(reversed(recovered.signatures))
            transactions = []
        if not recovered.complete:
            now = real_utc_now_iso()
            current = load_frontier(conn, source_key)
            newest_observed_cursor = _newest_solana_cursor(
                live_latest_cursor,
                current.latest_observed_cursor,
            )
            recoverable_gap_ids = tuple(
                row["gap_id"]
                for row in list_open_gaps(conn, source_key)
                if row["gap_kind"] != GapKind.DECODE_FAILURE.value
            )
            apply_frontier_batch(
                conn,
                FrontierBatch(
                    source_key=source_key,
                    expected_version=current.version,
                    previous_durable_cursor=current.durable_cursor,
                    newest_observed_cursor=newest_observed_cursor,
                    contiguous_durable_cursor=current.durable_cursor,
                    resume_cursor=recovered.resume_cursor or current.resume_cursor or "bounded",
                    reached_previous_frontier=False,
                    enumeration_complete=False,
                    decode_complete=True,
                    observed_at=now,
                    gap_kind=GapKind.ENUMERATION_BOUND,
                    gap_detail=f"recovery exceeded {max_pages} pages",
                    resolved_gap_ids=recoverable_gap_ids,
                ),
            )
            runtime.heartbeat(source_key, status="degraded", blocker_code="RECOVERY_INCOMPLETE")
            return False
        if not indexed_recovery:
            transactions = await _fetch_solana_transactions(
                rpc_url, signatures, rpc_governor=rpc_governor
            )
        for signature, tx in zip(signatures, transactions):
            if not isinstance(tx, dict):
                raise RuntimeError(f"missing confirmed transaction {signature}")
            received = real_utc_now_iso()
            slot = int(tx["slot"])
            # Live capture intentionally keeps transaction position and block
            # identity out of its fast-path identity. Recovery must use the
            # same fields or the same event becomes a second canonical event.
            # The transaction response already carries blockTime, so a second
            # getBlock call only doubles recovery pressure.
            transaction_index = None
            block_hash = None
            block_time = tx.get("blockTime")
            logs = ((tx.get("meta") or {}).get("logMessages") or [])
            keys = (((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
            first = keys[0] if keys else None
            actor = first.get("pubkey") if isinstance(first, dict) else first
            event_logs = (
                program_cpi_event_logs(tx, program_id)
                if venue is VenueId.BAGS_SOLANA
                else program_data_logs(logs, program_id)
            )
            for event_index, log_line in event_logs:
                if (
                    venue is VenueId.PUMPFUN_SOLANA
                    and is_known_irrelevant_pumpfun_event(log_line)
                ) or (
                    venue is VenueId.BAGS_SOLANA
                    and is_known_irrelevant_bags_solana_event(log_line)
                ):
                    continue
                source_cursor = f"{slot}:{signature}:{event_index}"
                if conn.execute(
                    "SELECT 1 FROM op_raw_events WHERE source_key=? AND source_cursor=?",
                    (source_key, source_cursor),
                ).fetchone() is not None:
                    continue
                payload = {"log_line": log_line, "actor_id": actor}
                token_hint = program_id
                if venue is VenueId.BAGS_SOLANA and bags_pool_mints:
                    decoded = decode_bags_solana_event(
                        BAGS_DBC_PROGRAM,
                        log_line,
                        token_by_pool=bags_pool_mints,
                        actor_id=str(actor) if actor else None,
                    )
                    if decoded.blocker is DecodeBlocker.MISSING_IDENTITY:
                        continue
                    if decoded.blocker is None and decoded.token_id and decoded.curve_id:
                        payload["pool_id"] = decoded.curve_id
                        token_hint = decoded.token_id
                runtime.submit(
                    source_key,
                    OpportunitySourceEnvelope(
                        source_key=source_key,
                        source_provider="solana_rpc_recovery",
                        source_cursor=source_cursor,
                        venue_id=venue,
                        program_or_contract_id=program_id,
                        transaction_id=signature,
                        block_or_slot=slot,
                        transaction_index=transaction_index,
                        event_index=event_index,
                        block_hash=block_hash,
                        chain_timestamp=_chain_iso(block_time, received),
                        received_at=received,
                        confirmation=ConfirmationState.TENTATIVE,
                        payload=payload,
                        token_hint=token_hint,
                    ),
                )
                runtime.drain(max_items=1)
        now = real_utc_now_iso()
        current = load_frontier(conn, source_key)
        newest_observed_cursor = _newest_solana_cursor(
            live_latest_cursor,
            current.latest_observed_cursor,
        )
        _finish_recovery(
            conn,
            runtime,
            source_key,
            observed_at=now,
            newest_observed_cursor=newest_observed_cursor,
        )
        return True
    except Exception as exc:
        record_source_failure(
            conn,
            source_key=source_key,
            observed_at=real_utc_now_iso(),
            detail=f"recovery {type(exc).__name__}: {str(exc)[:300]}",
        )
        runtime.heartbeat(source_key, status="degraded", blocker_code="RECOVERY_INCOMPLETE")
        return False


def _cursor_block(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        return int(cursor.split(":", 1)[0])
    except (TypeError, ValueError):
        return None


async def _recover_evm_frontier(
    conn,
    runtime,
    *,
    rpc_url: str,
    max_pages: int,
    chunk_blocks: int = 500,
    rpc_governor=None,
    abandon_fn=None,
    venue: VenueId = VenueId.BAGS_ROBINHOOD_4663,
    identities: VenueIdentityMap | None = None,
) -> None:
    venue = VenueId(venue)
    # BEFORE any RPC call. A retired venue selects an empty topic tuple below, and
    # `topics: [[]]` is a WILDCARD in eth_getLogs -- it would request every log on the
    # chain rather than none. Recovery for a retired venue must not run at all.
    if not is_active_capture_venue(venue):
        return
    source_key = f"{venue.value}:ws"
    identities = identities or VenueIdentityMap()
    frontier = load_frontier(conn, source_key)
    if frontier is None:
        runtime.mark_recovered(source_key)
        return
    start = _cursor_block(frontier.resume_cursor) or _cursor_block(frontier.durable_cursor)
    if start is None:
        record_source_failure(
            conn, source_key=source_key, observed_at=real_utc_now_iso(),
            detail="EVM recovery cursor is not block-addressable",
        )
        runtime.heartbeat(source_key, status="degraded", blocker_code="RECOVERY_INCOMPLETE")
        return
    topics = (
        (PONS_TOKEN_LAUNCHED, PONS_V3_SWAP)
        if venue is VenueId.PONS_V1_ROBINHOOD_4663
        else _BAGS_4663_TOPICS
    )
    try:
        head = int(await _rpc_call(rpc_governor, RpcPriority.CURRENT, rpc_url, "eth_blockNumber", []), 16)
        current_block = start
        pages = 0
        while current_block <= head and pages < max_pages:
            end = min(head, current_block + chunk_blocks - 1)
            logs = await _rpc_call(
                rpc_governor, RpcPriority.RECOVERY, rpc_url, "eth_getLogs",
                [{"fromBlock": hex(current_block), "toBlock": hex(end), "topics": [list(topics)]}],
            ) or []
            ordered = sorted(
                logs,
                key=lambda log: (
                    int(log["blockNumber"], 16),
                    int(log["transactionIndex"], 16),
                    int(log["logIndex"], 16),
                ),
            )
            pons_pools = dict(identities.pons_pool_tokens)
            if venue is VenueId.PONS_V1_ROBINHOOD_4663:
                for log in ordered:
                    decoded = decode_pons_4663_log(log, pool_tokens=pons_pools)
                    pair_token = decoded.facts.get("pair_token")
                    if (
                        decoded.event_kind is EventKind.LAUNCH
                        and decoded.pool_id and decoded.token_id and pair_token
                    ):
                        remember_pons_pool(
                            pons_pools,
                            decoded.pool_id,
                            decoded.token_id,
                            str(pair_token),
                        )
            block_times: dict[str, int] = {}
            for log in ordered:
                if (
                    venue is VenueId.PONS_V1_ROBINHOOD_4663
                    and _evm_log_venue(log, pons_pools) is not venue
                ):
                    continue
                if (
                    venue is VenueId.PONS_V1_ROBINHOOD_4663
                    and is_known_irrelevant_pons_swap(log)
                ):
                    continue
                parsed = parse_evm_log_notification({"params": {"result": log}})
                if parsed is None:
                    raise RuntimeError("unparseable EVM recovery log")
                cursor = f"{parsed['block_or_slot']}:{parsed['transaction_index']}:{parsed['event_index']}"
                if frontier.durable_cursor and cursor == frontier.durable_cursor:
                    continue
                block_hash = parsed["block_hash"]
                if block_hash not in block_times:
                    header = await _rpc_call(
                        rpc_governor, RpcPriority.RECOVERY, rpc_url,
                        "eth_getBlockByHash", [block_hash, False]
                    )
                    block_times[block_hash] = int(header["timestamp"], 16)
                received = real_utc_now_iso()
                address = str(log.get("address") or "").lower()
                token_hint = address
                if venue is VenueId.PONS_V1_ROBINHOOD_4663:
                    if address == PONS_FACTORY:
                        token_hint = (
                            decode_pons_4663_log(log, pool_tokens=pons_pools).token_id
                            or address
                        )
                    else:
                        token_hint = pons_pools.get(address, (address, ""))[0]
                runtime.submit(
                    source_key,
                    OpportunitySourceEnvelope(
                        source_key=source_key,
                        source_provider="eth_getLogs_recovery",
                        source_cursor=cursor,
                        venue_id=venue,
                        program_or_contract_id=address,
                        transaction_id=parsed["transaction_id"],
                        block_or_slot=parsed["block_or_slot"],
                        transaction_index=parsed["transaction_index"],
                        event_index=parsed["event_index"],
                        block_hash=block_hash,
                        chain_timestamp=_chain_iso(block_times[block_hash], received),
                        received_at=received,
                        confirmation=ConfirmationState.CONFIRMED,
                        payload={
                            "log": log,
                            **(
                                {
                                    "pons_token_id": pons_pools[address][0],
                                    "pons_pair_token": pons_pools[address][1],
                                }
                                if venue is VenueId.PONS_V1_ROBINHOOD_4663
                                and address in pons_pools else {}
                            ),
                        },
                        token_hint=token_hint,
                    ),
                )
                runtime.drain(max_items=1)
            current_block = end + 1
            pages += 1
        now = real_utc_now_iso()
        if current_block <= head:
            current = load_frontier(conn, source_key)
            apply_frontier_batch(
                conn,
                FrontierBatch(
                    source_key=source_key,
                    expected_version=current.version,
                    previous_durable_cursor=current.durable_cursor,
                    newest_observed_cursor=current.latest_observed_cursor,
                    contiguous_durable_cursor=current.durable_cursor,
                    resume_cursor=f"{current_block}:recovery:0",
                    reached_previous_frontier=False,
                    enumeration_complete=False,
                    decode_complete=True,
                    observed_at=now,
                    gap_kind=GapKind.ENUMERATION_BOUND,
                    gap_detail=f"recovery exceeded {max_pages} block chunks",
                ),
            )
            runtime.heartbeat(source_key, status="degraded", blocker_code="RECOVERY_INCOMPLETE")
            if abandon_fn is not None:
                abandon_fn(venue, now)
            return
        _finish_recovery(conn, runtime, source_key, observed_at=now)
    except Exception as exc:
        record_source_failure(
            conn,
            source_key=source_key,
            observed_at=real_utc_now_iso(),
            detail=f"recovery {type(exc).__name__}: {str(exc)[:300]}",
        )
        runtime.heartbeat(source_key, status="degraded", blocker_code="RECOVERY_INCOMPLETE")


async def _solana_event_context(
    *,
    venue: VenueId,
    rpc_url: str,
    signature: str,
    slot: int,
    block_cache: dict[int, dict],
) -> tuple[int | None, str | None, int | None, str | None]:
    # Program logs already carry every Level-1 Pump fact. Native ordering and the
    # Bags signer are enrichment, never a reason to stall or drop the fast stream.
    return None, None, None, None


async def _solana_lane(
    runtime,
    *,
    venue,
    program_id,
    ws_url,
    rpc_url,
    recovery_max_pages,
    stop,
    block_cache=None,
    rpc_governor=None,
):
    import websockets

    source_key = f"{venue.value}:ws"
    block_cache = {} if block_cache is None else block_cache
    while not stop.is_set():
        recovery_task = None
        try:
            async with websockets.connect(ws_url, open_timeout=10, ping_timeout=30) as ws:
                await ws.send(solana_program_subscribe_frame(program_id, request_id=1))
                subscription = None
                recovered_after_subscribe = False
                while not stop.is_set():
                    try:
                        raw_message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if subscription is not None:
                            _source_heartbeat(runtime, source_key)
                        continue
                    message = json.loads(raw_message)
                    if "result" in message and message.get("id") == 1:
                        subscription = message["result"]
                        _source_heartbeat(runtime, source_key)
                        if not recovered_after_subscribe:
                            recovered_after_subscribe = True
                            # Recovery is the lowest RPC priority (RECOVERY=3) on a
                            # paced, concurrency-1 pool, so it can run for hours.
                            # Awaiting it here parked the whole lane: no idle
                            # heartbeat, no live events, and a healthy subscribed
                            # socket was indistinguishable from a dead one
                            # (bags_solana:ws went silent 5.2h on 2026-07-30).
                            # `_finish_recovery` already reconciles a live tail that
                            # advanced during replay, so the loop keeps serving.
                            recovery_task = asyncio.create_task(
                                _recover_solana_frontier(
                                    runtime.conn,
                                    runtime,
                                    venue=venue,
                                    program_id=program_id,
                                    rpc_url=rpc_url,
                                    max_pages=recovery_max_pages,
                                    rpc_governor=rpc_governor,
                                )
                            )
                            await asyncio.sleep(0)
                        continue
                    parsed = parse_solana_log_notification(message)
                    if parsed is None or (subscription is not None and parsed["subscription"] != subscription):
                        continue
                    await _submit_solana_parsed(
                        runtime,
                        venue=venue,
                        program_id=program_id,
                        rpc_url=rpc_url,
                        parsed=parsed,
                        block_cache=block_cache,
                        rpc_governor=rpc_governor,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _report_lane_failure(
                runtime, source_key, f"{type(exc).__name__}: {str(exc)[:300]}"
            )
            await asyncio.sleep(1.0)
        finally:
            # One recovery per connection. A reconnect re-anchors from the durable
            # cursor, so an in-flight replay against the dead socket's frontier is
            # abandoned rather than raced against the new one.
            if recovery_task is not None and not recovery_task.done():
                recovery_task.cancel()
                try:
                    await recovery_task
                except (asyncio.CancelledError, Exception):
                    pass


async def _submit_solana_parsed(
    runtime, *, venue, program_id, rpc_url, parsed, block_cache, rpc_governor=None,
    expected_pool: str | None = None, pool_mints: dict[str, str] | None = None,
) -> None:
    if parsed["err"] is not None:
        return
    received = real_utc_now_iso()
    transaction_index, block_hash, block_time, actor = await _solana_event_context(
        venue=venue,
        rpc_url=rpc_url,
        signature=parsed["signature"],
        slot=parsed["slot"],
        block_cache=block_cache,
    )
    transaction = None
    if venue is VenueId.BAGS_SOLANA:
        if (
            not expected_pool
            or not pool_mints
            or expected_pool not in pool_mints
        ):
            return
        transactions = await _fetch_solana_transactions(
            rpc_url,
            [parsed["signature"]],
            rpc_governor=rpc_governor,
        )
        transaction = transactions[0] if transactions else None
        if not isinstance(transaction, dict):
            raise RuntimeError(f"missing confirmed transaction {parsed['signature']}")
        block_time = transaction.get("blockTime")
        keys = (((transaction.get("transaction") or {}).get("message") or {}).get("accountKeys") or [])
        first = keys[0] if keys else None
        actor = first.get("pubkey") if isinstance(first, dict) else first
    source_key = f"{venue.value}:ws"
    event_logs = (
        program_cpi_event_logs(transaction, program_id)
        if venue is VenueId.BAGS_SOLANA
        else program_data_logs(parsed["logs"], program_id)
    )
    for event_index, log_line in event_logs:
        if (
            venue is VenueId.PUMPFUN_SOLANA
            and is_known_irrelevant_pumpfun_event(log_line)
        ) or (
            venue is VenueId.BAGS_SOLANA
            and is_known_irrelevant_bags_solana_event(log_line)
        ):
            continue
        if venue is VenueId.BAGS_SOLANA:
            decoded = decode_bags_solana_event(
                BAGS_DBC_PROGRAM,
                log_line,
                token_by_pool=pool_mints,
                actor_id=str(actor) if actor else None,
            )
            if (
                decoded.blocker is not None
                or decoded.curve_id != expected_pool
                or decoded.token_id != pool_mints.get(expected_pool)
            ):
                continue
        runtime.submit(
            source_key,
            OpportunitySourceEnvelope(
                source_key=source_key,
                source_provider="solana_logs_subscribe",
                source_cursor=f"{parsed['slot']}:{parsed['signature']}:{event_index}",
                venue_id=venue,
                program_or_contract_id=program_id,
                transaction_id=parsed["signature"],
                block_or_slot=parsed["slot"],
                transaction_index=transaction_index,
                event_index=event_index,
                block_hash=block_hash,
                chain_timestamp=_chain_iso(block_time, received),
                received_at=received,
                confirmation=ConfirmationState.TENTATIVE,
                payload={
                    "log_line": log_line,
                    "actor_id": actor,
                    "pool_id": expected_pool,
                },
                token_hint=(
                    pool_mints[expected_pool]
                    if venue is VenueId.BAGS_SOLANA and expected_pool and pool_mints
                    else program_id
                ),
            ),
        )


def _combined_rpc_metrics(primary, recovery, robinhood):
    """Report every governed HTTP call without mixing provider rate controls."""
    providers = (primary, recovery, robinhood)
    return {
        "current_rps": round(sum(float(item["current_rps"]) for item in providers), 3),
        "configured_concurrency": sum(
            int(item["configured_concurrency"]) for item in providers
        ),
        "active_calls": sum(int(item["active_calls"]) for item in providers),
        "max_observed_concurrency": sum(
            int(item["max_observed_concurrency"]) for item in providers
        ),
        "queue_depth": sum(int(item["queue_depth"]) for item in providers),
        "rate_limit_events": sum(
            int(item["rate_limit_events"]) for item in providers
        ),
        "throttled": any(bool(item["throttled"]) for item in providers),
        "primary": dict(primary),
        "recovery": dict(recovery),
        "robinhood": dict(robinhood),
    }


def _quote_path_status_rows(
    *,
    solana_metrics: dict[str, object],
    evm_metrics: dict[str, object],
    now_iso: str,
    stale_after_s: float = 120.0,
) -> dict[str, dict[str, object]]:
    """Grade direct quote work without using an alert as a proxy."""
    evm_by_venue = dict(evm_metrics.get("by_venue") or {})
    raw = {
        VenueId.PUMPFUN_SOLANA.value: {
            **dict(solana_metrics),
            "queue_depth": int(solana_metrics.get("queued") or 0)
            + int(solana_metrics.get("paper_queued") or 0),
        },
        VenueId.BAGS_ROBINHOOD_4663.value: dict(
            evm_by_venue.get(VenueId.BAGS_ROBINHOOD_4663.value) or {}
        ),
        VenueId.PONS_V1_ROBINHOOD_4663.value: dict(
            evm_by_venue.get(VenueId.PONS_V1_ROBINHOOD_4663.value) or {}
        ),
    }
    rows = {}
    now = _parse_iso(now_iso)
    for venue, progress in raw.items():
        requested = int(progress.get("requested") or 0)
        pending = int(progress.get("pending") or 0)
        last_request_at = progress.get("last_request_at")
        last_success_at = progress.get("last_success_at")
        last_failure_at = progress.get("last_failure_at")
        failure_is_current = bool(
            last_failure_at
            and (
                not last_success_at
                or _parse_iso(str(last_failure_at)) > _parse_iso(str(last_success_at))
            )
        )
        pending_is_stale = bool(
            pending > 0
            and last_request_at
            and (now - _parse_iso(str(last_request_at))).total_seconds()
            > float(stale_after_s)
        )
        blocker_code = (
            "QUOTE_PATH_STALE"
            if pending_is_stale
            else "QUOTE_PATH_FAILURE"
            if failure_is_current
            else None
        )
        state = (
            "idle"
            if requested == 0
            else "stale"
            if pending_is_stale
            else "failed"
            if failure_is_current
            else "current"
        )
        detail = {
            **progress,
            "state": state,
            "stale_after_s": float(stale_after_s),
        }
        rows[venue] = {
            "status": "degraded" if blocker_code else "healthy",
            "blocker_code": blocker_code,
            "queue_depth": int(progress.get("queue_depth") or pending),
            "detail": detail,
        }
    return rows


def _publish_quote_path_status_rows(
    conn,
    rows: dict[str, dict[str, object]],
    *,
    now_iso: str,
) -> None:
    for venue, row in rows.items():
        # A retired venue writes no quote status. Its old row stays for history.
        if not is_active_capture_venue(venue):
            continue
        upsert_runtime_status(
            conn,
            source_key=f"opportunity:quote:{venue}",
            status=str(row["status"]),
            blocker_code=row.get("blocker_code"),
            heartbeat_at=now_iso,
            queue_depth=int(row.get("queue_depth") or 0),
            detail=dict(row.get("detail") or {}),
        )


def _pump_recovery_transport(settings, primary_governor, recovery_governor):
    return (
        settings.solana_rpc_url
        if settings.solana_indexed_recovery
        else settings.solana_recovery_rpc_url,
        recovery_governor,
        settings.solana_indexed_recovery,
    )


def _solana_confirmation_transport(settings, primary_governor, recovery_governor):
    del primary_governor
    return settings.solana_recovery_rpc_url, recovery_governor


def _solana_ws_transport(settings):
    return settings.solana_ws_url or _ws_from_http(settings.solana_rpc_url)


async def _solana_recovery_loop(
    runtime,
    *,
    venue: VenueId,
    program_id: str,
    rpc_url: str,
    max_pages: int,
    stop: asyncio.Event,
    indexed_recovery: bool = False,
    rpc_governor=None,
    abandon_fn=None,
    bags_pool_mints: dict[str, str] | None = None,
) -> None:
    source_key = f"{venue.value}:ws"
    if venue is VenueId.BAGS_SOLANA:
        _retry_stored_solana_decode_gaps(
            runtime.conn,
            runtime,
            source_key=source_key,
            venue=venue,
            program_id=program_id,
            bags_pool_mints=bags_pool_mints,
        )
        if (
            abandon_fn is not None
            and runtime.source_gapped(source_key)
            and frontier_gap_is_discardable(runtime.conn, source_key)
        ):
            abandon_fn(venue, real_utc_now_iso())
        return
    if not runtime.source_gapped(source_key):
        observed_at = real_utc_now_iso()
        record_source_failure(
            runtime.conn,
            source_key=source_key,
            observed_at=observed_at,
            detail="worker restart requires source continuity recovery",
        )
        runtime.heartbeat(
            source_key,
            status="degraded",
            blocker_code="RECOVERY_INCOMPLETE",
        )
    # Checkpoint each provider page before requesting another. This keeps a
    # public-RPC slowdown out of the WebSocket receive path and makes every
    # successful page resumable after cancellation or reconnect.
    while not stop.is_set():
        for _ in range(max_pages):
            if stop.is_set():
                return
            complete = await _recover_solana_frontier(
                runtime.conn,
                runtime,
                venue=venue,
                program_id=program_id,
                rpc_url=rpc_url,
                max_pages=1,
                indexed_recovery=indexed_recovery,
                rpc_governor=rpc_governor,
                bags_pool_mints=bags_pool_mints,
            )
            if complete or not runtime.source_gapped(f"{venue.value}:ws"):
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
        if (
            abandon_fn is not None
            and not stop.is_set()
            and runtime.source_gapped(f"{venue.value}:ws")
        ):
            abandon_fn(venue, real_utc_now_iso())
        return


async def _solana_multi_lane(
    runtime, *, ws_url, rpc_url, recovery_rpc_url=None, recovery_max_pages, stop, block_cache,
    rpc_governor=None, bags_pool_mints: dict[str, str] | None = None,
    recovery_rpc_governor=None,
    indexed_recovery: bool = False,
    abandon_fn=None,
    include_pump: bool = True,
    bags_shard_index: int = 0,
    bags_shard_count: int = 1,
    timings: _RuntimeTimingMetrics | None = None,
) -> None:
    import websockets

    bags_pool_mints = bags_pool_mints if bags_pool_mints is not None else {}
    last_health_write_s: dict[str, float] = {}

    def persist_transport_heartbeat(source_key: str, *, force: bool = False) -> None:
        now_s = time.monotonic()
        prior_s = last_health_write_s.get(source_key)
        if not force and prior_s is not None and now_s - prior_s < 1.0:
            return
        _source_heartbeat(runtime, source_key)
        last_health_write_s[source_key] = now_s

    # Which venues this lane is responsible for, from its configuration rather than
    # from how much work it currently holds. The pump lane runs with include_pump and
    # an empty pool map; a bags shard runs without it.
    owned_venue = VenueId.PUMPFUN_SOLANA if include_pump else VenueId.BAGS_SOLANA
    owned_source_key = f"{owned_venue.value}:ws"

    def heartbeat_owned_transport(active_subscriptions: int, pending: int) -> None:
        """Report transport liveness from this loop, never from the subscription set.

        Deriving it from `subscriptions.values()` meant a lane whose subscription set
        emptied stopped writing a heartbeat at all, so a connected socket that was
        simply subscribed to nothing was indistinguishable from a dead transport --
        `bags_solana:ws` sat frozen for hours with no gap, no traceback and a healthy
        feed. Same confusion as the 900s event bound on a quiet venue and the
        recovery lane that only beat when it had work.

        An empty subscription set is still not healthy capture, so say which it is
        instead of going silent: the socket is up, and we are subscribed to nothing.
        """
        if (
            owned_venue is VenueId.BAGS_SOLANA
            and bags_pool_mints
            and active_subscriptions == 0
        ):
            runtime.heartbeat(
                owned_source_key,
                status="degraded",
                blocker_code="BAGS_SUBSCRIPTIONS_EMPTY",
                detail={
                    "transport_stale_after_s": _SOLANA_TRANSPORT_STALE_AFTER_S,
                    "subscribed_pools": active_subscriptions,
                    "pending_unsubscribes": pending,
                    "desired_pools": sum(
                        1 for pool in bags_pool_mints
                        if _bags_pool_shard(pool, bags_shard_count) == bags_shard_index
                    ),
                },
            )
            last_health_write_s[owned_source_key] = time.monotonic()
            return
        persist_transport_heartbeat(owned_source_key)

    while not stop.is_set():
        try:
            async with websockets.connect(
                ws_url,
                open_timeout=10,
                ping_interval=20,
                # A bounded-gap quarantine can synchronously rebuild the hot
                # state for longer than one ping interval. Keep the socket
                # alive through that local pause instead of creating a new gap.
                ping_timeout=60,
                # Pump can deliver more than 2,000 notifications while the
                # bounded quarantine rebuild runs. Preserve that live tail.
                max_queue=4096,
            ) as ws:
                pending_requests = (
                    {1: (VenueId.PUMPFUN_SOLANA, PUMPFUN_PROGRAM, None)}
                    if include_pump
                    else {}
                )
                subscriptions: dict[int, tuple[VenueId, str, str | None]] = {}
                pending_unsubscribes: dict[int, int] = {}
                next_request_id = 2 if include_pump else 1
                if include_pump:
                    await ws.send(
                        solana_program_subscribe_frame(PUMPFUN_PROGRAM, request_id=1)
                    )

                async def sync_bags_pool_subscriptions() -> None:
                    nonlocal next_request_id
                    desired = {
                        pool for pool in bags_pool_mints
                        if _bags_pool_shard(pool, bags_shard_count) == bags_shard_index
                    }
                    pending_subscription_ids = set(pending_unsubscribes.values())
                    for subscription_id, config in tuple(subscriptions.items()):
                        venue, _program, pool = config
                        if (
                            venue is not VenueId.BAGS_SOLANA
                            or pool in desired
                            or subscription_id in pending_subscription_ids
                        ):
                            continue
                        request_id = next_request_id
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "logsUnsubscribe",
                            "params": [subscription_id],
                        }, separators=(",", ":")))
                        pending_unsubscribes[request_id] = subscription_id
                        next_request_id += 1
                    active = {
                        pool
                        for _venue, _program, pool in (
                            *pending_requests.values(), *subscriptions.values()
                        )
                        if pool is not None
                    }
                    if pending_unsubscribes:
                        return
                    bags_subscription_count = sum(
                        venue is VenueId.BAGS_SOLANA
                        for venue, _program, _pool in (
                            *pending_requests.values(), *subscriptions.values()
                        )
                    )
                    available = max(
                        0,
                        _BAGS_WS_SUBSCRIPTION_CAP - bags_subscription_count,
                    )
                    for pool in sorted(desired - active)[:available]:
                        request_id = next_request_id
                        next_request_id += 1
                        pending_requests[request_id] = (
                            VenueId.BAGS_SOLANA,
                            BAGS_DBC_PROGRAM,
                            pool,
                        )
                        await ws.send(
                            solana_program_subscribe_frame(pool, request_id=request_id)
                        )

                await sync_bags_pool_subscriptions()
                recovery_tasks: dict[VenueId, asyncio.Task] = {}

                def ensure_recovery(
                    venue: VenueId, program_id: str, *, force: bool = False
                ) -> None:
                    owns_source = (
                        venue is VenueId.PUMPFUN_SOLANA
                        or bags_shard_index == 0
                    )
                    if not owns_source:
                        return
                    current = recovery_tasks.get(venue)
                    if current is not None and not current.done():
                        return
                    if current is not None:
                        if not current.cancelled():
                            try:
                                current.result()
                            except Exception:
                                pass
                        if not force and not runtime.source_gapped(f"{venue.value}:ws"):
                            return
                    elif not force and not runtime.source_gapped(f"{venue.value}:ws"):
                        return
                    recovery_tasks[venue] = asyncio.create_task(
                        _solana_recovery_loop(
                            runtime,
                            venue=venue,
                            program_id=program_id,
                            rpc_url=recovery_rpc_url or rpc_url,
                            max_pages=recovery_max_pages,
                            stop=stop,
                            indexed_recovery=indexed_recovery,
                            rpc_governor=(recovery_rpc_governor or rpc_governor),
                            abandon_fn=abandon_fn,
                            bags_pool_mints=(
                                bags_pool_mints
                                if venue is VenueId.BAGS_SOLANA else None
                            ),
                        )
                    )

                try:
                    while not stop.is_set():
                        await sync_bags_pool_subscriptions()
                        for venue, program_id, _pool in subscriptions.values():
                            ensure_recovery(venue, program_id)
                        try:
                            raw_message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            heartbeat_owned_transport(
                                len(subscriptions), len(pending_unsubscribes)
                            )
                            continue
                        message = json.loads(raw_message)
                        request_id = message.get("id")
                        if "result" in message and request_id in pending_unsubscribes:
                            subscription_id = pending_unsubscribes.pop(request_id)
                            subscriptions.pop(subscription_id, None)
                            continue
                        if "result" in message and request_id in pending_requests:
                            venue, program_id, expected_pool = pending_requests.pop(request_id)
                            subscriptions[message["result"]] = (
                                venue, program_id, expected_pool
                            )
                            persist_transport_heartbeat(
                                f"{venue.value}:ws", force=True
                            )
                            ensure_recovery(venue, program_id, force=True)
                            await asyncio.sleep(0)
                            continue
                        # All subscriptions share this live transport. Traffic for a
                        # busy program proves the quiet subscription's socket is
                        # still connected too; do not let it age out waiting for a
                        # whole-socket idle timeout that may never occur.
                        for subscribed_venue, _program, _pool in subscriptions.values():
                            persist_transport_heartbeat(
                                f"{subscribed_venue.value}:ws"
                            )
                        parsed = parse_solana_log_notification(message)
                        config = subscriptions.get(parsed["subscription"]) if parsed else None
                        if config is None:
                            continue
                        venue, program_id, expected_pool = config
                        if (
                            venue is VenueId.BAGS_SOLANA
                            and expected_pool not in bags_pool_mints
                        ):
                            continue
                        lane_started_s = time.monotonic()
                        try:
                            await _submit_solana_parsed(
                                runtime,
                                venue=venue,
                                program_id=program_id,
                                rpc_url=rpc_url,
                                parsed=parsed,
                                block_cache=block_cache,
                                rpc_governor=rpc_governor,
                                expected_pool=expected_pool,
                                pool_mints=bags_pool_mints,
                            )
                        finally:
                            if timings is not None and venue is VenueId.PUMPFUN_SOLANA:
                                timings.observe(
                                    "pump_lane",
                                    time.monotonic() - lane_started_s,
                                    limit_s=1.0,
                                )
                finally:
                    for task in recovery_tasks.values():
                        task.cancel()
                    if recovery_tasks:
                        await asyncio.gather(
                            *recovery_tasks.values(), return_exceptions=True
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            impacted = [VenueId.PUMPFUN_SOLANA] if include_pump else []
            if bags_pool_mints:
                impacted.append(VenueId.BAGS_SOLANA)
            for venue in impacted:
                source_key = f"{venue.value}:ws"
                _report_lane_failure(
                    runtime, source_key, f"{type(exc).__name__}: {str(exc)[:300]}"
                )
            await asyncio.sleep(1.0)


async def _evm_lane(
    runtime,
    *,
    ws_url,
    rpc_url,
    recovery_max_pages,
    stop,
    rpc_governor=None,
    abandon_fn=None,
    identities: VenueIdentityMap | None = None,
):
    import websockets

    identities = identities or VenueIdentityMap()
    source_keys = ("bags_robinhood_4663:ws", "pons_v1_4663:ws")
    topics = _EVM_CAPTURE_TOPICS
    block_times: dict[str, int] = {}
    while not stop.is_set():
        try:
            async with websockets.connect(ws_url, open_timeout=10, ping_timeout=30) as ws:
                await ws.send(evm_logs_subscribe_frame(topics, request_id=1))
                subscription_ready = False
                recovered_after_subscribe = False
                while not stop.is_set():
                    try:
                        raw_message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if subscription_ready and recovered_after_subscribe:
                            for source_key in source_keys:
                                _source_heartbeat(runtime, source_key)
                        continue
                    message = json.loads(raw_message)
                    if "result" in message and message.get("id") == 1:
                        subscription_ready = True
                        for source_key in source_keys:
                            _source_heartbeat(runtime, source_key)
                        if not recovered_after_subscribe:
                            if (
                                abandon_fn is not None
                                and frontier_gap_is_discardable(
                                    runtime.conn, "bags_robinhood_4663:ws"
                                )
                            ):
                                abandon_fn(
                                    VenueId.BAGS_ROBINHOOD_4663,
                                    real_utc_now_iso(),
                                )
                            else:
                                await _recover_evm_frontier(
                                    runtime.conn,
                                    runtime,
                                    rpc_url=rpc_url,
                                    max_pages=recovery_max_pages,
                                    rpc_governor=rpc_governor,
                                    abandon_fn=abandon_fn,
                                    venue=VenueId.BAGS_ROBINHOOD_4663,
                                    identities=identities,
                                )
                            if (
                                hasattr(runtime.conn, "execute")
                                and load_frontier(runtime.conn, "pons_v1_4663:ws") is not None
                            ):
                                if (
                                    abandon_fn is not None
                                    and frontier_gap_is_discardable(
                                        runtime.conn, "pons_v1_4663:ws"
                                    )
                                ):
                                    abandon_fn(
                                        VenueId.PONS_V1_ROBINHOOD_4663,
                                        real_utc_now_iso(),
                                    )
                                else:
                                    await _recover_evm_frontier(
                                        runtime.conn,
                                        runtime,
                                        rpc_url=rpc_url,
                                        max_pages=recovery_max_pages,
                                        rpc_governor=rpc_governor,
                                        abandon_fn=abandon_fn,
                                        venue=VenueId.PONS_V1_ROBINHOOD_4663,
                                        identities=identities,
                                    )
                            recovered_after_subscribe = True
                        continue
                    parsed = parse_evm_log_notification(message)
                    if parsed is None:
                        continue
                    log = parsed["log"]
                    if (
                        str(log.get("address") or "").lower() == PONS_FACTORY
                        and str((log.get("topics") or [""])[0]).lower()
                        == PONS_TOKEN_LAUNCHED
                    ):
                        decoded = decode_pons_4663_log(
                            log, pool_tokens=identities.pons_pool_tokens
                        )
                        pair_token = decoded.facts.get("pair_token")
                        if decoded.blocker is None and decoded.pool_id and decoded.token_id and pair_token:
                            remember_pons_pool(
                                identities.pons_pool_tokens,
                                decoded.pool_id,
                                decoded.token_id,
                                str(pair_token),
                            )
                    venue = _evm_log_venue(log, identities.pons_pool_tokens)
                    if venue is None:
                        continue
                    if (
                        venue is VenueId.PONS_V1_ROBINHOOD_4663
                        and is_known_irrelevant_pons_swap(log)
                    ):
                        continue
                    source_key = f"{venue.value}:ws"
                    received = real_utc_now_iso()
                    block_hash = parsed["block_hash"]
                    if block_hash not in block_times:
                        header = await _rpc_call(
                            rpc_governor, RpcPriority.CURRENT, rpc_url,
                            "eth_getBlockByHash", [block_hash, False]
                        )
                        block_times[block_hash] = int(header["timestamp"], 16)
                        if len(block_times) > 512:
                            block_times.pop(next(iter(block_times)))
                    address = str(log.get("address") or "").lower()
                    token_hint = (
                        identities.pons_pool_tokens.get(address, (address, ""))[0]
                        if venue is VenueId.PONS_V1_ROBINHOOD_4663 else address
                    )
                    if venue is VenueId.PONS_V1_ROBINHOOD_4663 and address == PONS_FACTORY:
                        token_hint = decoded.token_id or address
                    envelope = OpportunitySourceEnvelope(
                        source_key=source_key,
                        source_provider="eth_subscribe_logs",
                        source_cursor=f"{parsed['block_or_slot']}:{parsed['transaction_index']}:{parsed['event_index']}",
                        venue_id=venue,
                        program_or_contract_id=address,
                        transaction_id=parsed["transaction_id"],
                        block_or_slot=parsed["block_or_slot"],
                        transaction_index=parsed["transaction_index"],
                        event_index=parsed["event_index"],
                        block_hash=block_hash,
                        chain_timestamp=_chain_iso(block_times[block_hash], received),
                        received_at=received,
                        confirmation=ConfirmationState(parsed["confirmation"]),
                        payload={"log": log},
                        token_hint=token_hint,
                    )
                    runtime.submit(source_key, envelope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for source_key in source_keys:
                _report_lane_failure(
                    runtime, source_key, f"{type(exc).__name__}: {str(exc)[:300]}"
                )
            await asyncio.sleep(1.0)


_ALL_BAGS_4663_TOPICS = (
    TOKEN_CREATED, TOKENS_BOUGHT, TOKENS_SOLD, MIGRATED, CLAIMERS_UPDATED,
)
_ALL_PONS_4663_TOPICS = (PONS_TOKEN_LAUNCHED, PONS_V3_SWAP)

# The producer uses the same venue authority as the read surfaces.
_TOPICS_BY_4663_VENUE = {
    VenueId.BAGS_ROBINHOOD_4663: _ALL_BAGS_4663_TOPICS,
    VenueId.PONS_V1_ROBINHOOD_4663: _ALL_PONS_4663_TOPICS,
}


def _active_4663_topics(venue: VenueId) -> tuple[str, ...]:
    """Topics for one 4663 venue, empty when that venue is not an active capture venue."""
    if not is_active_capture_venue(venue):
        return ()
    return _TOPICS_BY_4663_VENUE.get(venue, ())


_BAGS_4663_TOPICS = _active_4663_topics(VenueId.BAGS_ROBINHOOD_4663)
_PONS_4663_TOPICS = _active_4663_topics(VenueId.PONS_V1_ROBINHOOD_4663)
_EVM_CAPTURE_TOPICS = (*_BAGS_4663_TOPICS, *_PONS_4663_TOPICS)

# One shared scan cursor for the whole 4663 chain. Both venue filters are enumerated in
# the same bounded request, so the chain has ONE scan position and each venue keeps its
# own event frontier.
EVM_4663_CHAIN_ID = "robinhood_4663"


def _evm_log_venue(
    log: dict[str, object],
    pons_pools: dict[str, tuple[str, str]],
) -> VenueId | None:
    topics = [str(value).lower() for value in (log.get("topics") or [])]
    if not topics:
        return None
    address = str(log.get("address") or "").lower()
    if topics[0] == PONS_TOKEN_LAUNCHED and address == PONS_FACTORY:
        return VenueId.PONS_V1_ROBINHOOD_4663
    if topics[0] == PONS_V3_SWAP and address in pons_pools:
        return VenueId.PONS_V1_ROBINHOOD_4663
    if topics[0] in _BAGS_4663_TOPICS:
        return VenueId.BAGS_ROBINHOOD_4663
    return None


async def _evm_block_times(
    *,
    rpc_url: str,
    logs: list[dict[str, object]],
    canonical_through_block: int | None = None,
    rpc_governor=None,
    priority: RpcPriority = RpcPriority.CURRENT,
    on_progress=None,
) -> dict[str, int]:
    """Resolve exact monotonic block timestamps without fetching every header.

    `on_progress` fires after each completed header batch so a caller can prove the
    lane is still advancing. It must never fire on a timer -- a batch that never
    returns must produce no callback, or a hung poll would report itself alive.
    """
    refs_by_hash: dict[str, int] = {}
    for log in logs:
        block_hash = str(log.get("blockHash") or "")
        block_number = int(str(log.get("blockNumber") or ""), 16)
        if not block_hash:
            raise RuntimeError("EVM log is missing block hash")
        previous = refs_by_hash.setdefault(block_hash, block_number)
        if previous != block_number:
            raise RuntimeError("EVM block hash maps to conflicting heights")
    refs = sorted(refs_by_hash.items(), key=lambda item: (item[1], item[0]))
    if not refs:
        return {}

    timestamps: dict[int, int] = {}

    async def fetch(indices) -> None:
        missing = list(dict.fromkeys(index for index in indices if index not in timestamps))
        for offset in range(0, len(missing), _EVM_HEADER_BATCH_LIMIT):
            selected = missing[offset : offset + _EVM_HEADER_BATCH_LIMIT]
            headers = await _rpc_batch_call(
                rpc_governor,
                priority,
                rpc_url,
                [("eth_getBlockByHash", [refs[index][0], False]) for index in selected],
            )
            if len(headers) != len(selected):
                raise RuntimeError("EVM header batch response count mismatch")
            for index, header in zip(selected, headers):
                if not isinstance(header, dict) or "timestamp" not in header:
                    raise RuntimeError("EVM header is missing timestamp")
                timestamps[index] = int(str(header["timestamp"]), 16)
            if on_progress is not None:
                on_progress()          # one completed batch == real forward progress

    canonical_high = -1
    if canonical_through_block is not None:
        canonical_high = max(
            (index for index, (_hash, height) in enumerate(refs)
             if height <= canonical_through_block),
            default=-1,
        )
    await fetch(range(canonical_high + 1, len(refs)))
    if canonical_high >= 0:
        await fetch((0, canonical_high))
    segments = [(0, canonical_high)] if canonical_high >= 0 else []
    while segments:
        next_segments = []
        midpoints = []
        for low, high in segments:
            low_time = timestamps[low]
            high_time = timestamps[high]
            if low_time > high_time:
                raise RuntimeError("EVM block timestamps are not monotonic")
            if low_time == high_time:
                for index in range(low, high + 1):
                    timestamps[index] = low_time
                continue
            if high - low <= 1:
                continue
            middle = (low + high) // 2
            midpoints.append(middle)
            next_segments.extend(((low, middle), (middle, high)))
        if not midpoints:
            break
        await fetch(midpoints)
        segments = next_segments
    if len(timestamps) != len(refs):
        raise RuntimeError("EVM block timestamp resolution is incomplete")
    return {block_hash: timestamps[index] for index, (block_hash, _height) in enumerate(refs)}


async def _evm_poll_range(
    runtime,
    *,
    rpc_url: str,
    from_block: int,
    to_block: int,
    rpc_governor=None,
    identities: VenueIdentityMap | None = None,
    canonical_through_block: int | None = None,
    stage: list[str] | None = None,
    on_progress=None,
) -> dict[str, object]:
    """Fetch one bounded 4663 range and dispatch only known venue evidence.

    `stage` is an optional single-slot recorder the caller can read after a failure.
    A TimeoutError carries no message, so without it a bounded poll could report the
    range it died on but not whether it died fetching logs, resolving headers, or
    decoding and persisting -- which are wildly different faults.
    """
    def _mark(name: str) -> None:
        if stage is not None:
            stage[:] = [name]
        if on_progress is not None:
            on_progress()

    _mark("fetch_logs")
    rejected_submissions = 0
    drain_failures = 0
    range_keys: set[tuple[str, str]] = set()
    gapped_before = set(getattr(runtime, "gapped_sources", ()) or ())
    identities = identities or VenueIdentityMap()
    block_filter = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
    pons_pools = _retained_pons_pool_tokens(runtime, identities)
    known_pools = tuple(pons_pools)
    log_calls = []
    # A retired venue adds no call. `eth_getLogs` uses an empty topic array as a wildcard.
    if _BAGS_4663_TOPICS:
        log_calls.append((
            "eth_getLogs",
            [{**block_filter, "topics": [list(_BAGS_4663_TOPICS)]}],
        ))
    if PONS_TOKEN_LAUNCHED in _PONS_4663_TOPICS:
        log_calls.append((
            "eth_getLogs",
            [{
                **block_filter,
                "address": PONS_FACTORY,
                "topics": [[PONS_TOKEN_LAUNCHED]],
            }],
        ))
    if known_pools and PONS_V3_SWAP in _PONS_4663_TOPICS:
        log_calls.append(
            (
                "eth_getLogs",
                [{
                    **block_filter,
                    "address": list(known_pools),
                    "topics": [[PONS_V3_SWAP]],
                }],
            )
        )
    log_results = await _rpc_batch_call(
        rpc_governor,
        RpcPriority.CURRENT,
        rpc_url,
        log_calls,
    )
    logs = [log for result in log_results for log in (result or [])]
    _mark("decode_discovery")
    ordered = sorted(
        logs,
        key=lambda log: (
            int(log["blockNumber"], 16),
            int(log["transactionIndex"], 16),
            int(log["logIndex"], 16),
        ),
    )
    new_pools: list[str] = []
    for log in ordered:
        if (
            str(log.get("address") or "").lower() == PONS_FACTORY
            and str((log.get("topics") or [""])[0]).lower() == PONS_TOKEN_LAUNCHED
        ):
            decoded = decode_pons_4663_log(log, pool_tokens=pons_pools)
            pair_token = decoded.facts.get("pair_token")
            if decoded.blocker is None and decoded.pool_id and decoded.token_id and pair_token:
                if decoded.pool_id.lower() not in pons_pools:
                    new_pools.append(decoded.pool_id.lower())
                remember_pons_pool(
                    pons_pools,
                    decoded.pool_id,
                    decoded.token_id,
                    str(pair_token),
                )
    if new_pools:
        same_range_swaps = await _rpc_call(
            rpc_governor,
            RpcPriority.CURRENT,
            rpc_url,
            "eth_getLogs",
            [{
                **block_filter,
                "address": list(dict.fromkeys(new_pools)),
                "topics": [[PONS_V3_SWAP]],
            }],
        ) or []
        logs.extend(same_range_swaps)
        ordered = sorted(
            {
                (
                    str(log.get("blockHash") or ""),
                    str(log.get("transactionHash") or ""),
                    str(log.get("logIndex") or ""),
                ): log
                for log in logs
            }.values(),
            key=lambda log: (
                int(log["blockNumber"], 16),
                int(log["transactionIndex"], 16),
                int(log["logIndex"], 16),
            ),
        )
    accepted = [
        (log, venue)
        for log in ordered
        if (venue := _evm_log_venue(log, pons_pools)) is not None
        and not (
            venue is VenueId.PONS_V1_ROBINHOOD_4663
            and is_known_irrelevant_pons_swap(log)
        )
    ]
    _mark("resolve_headers")
    block_times = await _evm_block_times(
        rpc_url=rpc_url,
        logs=[log for log, _venue in accepted],
        canonical_through_block=canonical_through_block,
        rpc_governor=rpc_governor,
        on_progress=on_progress,
    )
    # Decode + persist. This phase is synchronous through `runtime.submit`/`drain`,
    # which is exactly the section the poll deadline cannot interrupt -- so naming it
    # is the difference between "the poll hung" and "the poll hung where nothing can
    # stop it".
    _mark("decode_persist")
    by_source: dict[str, dict[str, object]] = {}
    persist_batch_limit = _evm_poll_persist_batch_limit(runtime)
    pending_items = 0
    for log, venue in accepted:
        parsed = parse_evm_log_notification({"params": {"result": log}})
        if parsed is None:
            raise RuntimeError("unparseable EVM polled log")
        source_key = f"{venue.value}:ws"
        received = real_utc_now_iso()
        chain_timestamp = _chain_iso(block_times[parsed["block_hash"]], received)
        is_launch = str((log.get("topics") or [""])[0]).lower() in {
            TOKEN_CREATED, PONS_TOKEN_LAUNCHED,
        }
        activity = by_source.setdefault(
            source_key,
            {
                "events": 0,
                "launches": 0,
                "last_chain_event_at": None,
                "last_launch_at": None,
            },
        )
        activity["events"] = int(activity["events"]) + 1
        activity["last_chain_event_at"] = chain_timestamp
        if on_progress is not None:
            # Per persisted event. decode_persist is the long synchronous phase the
            # deadline cannot interrupt, so it is exactly where a working lane must be
            # able to prove it is still advancing.
            on_progress()
        if is_launch:
            activity["launches"] = int(activity["launches"]) + 1
            activity["last_launch_at"] = chain_timestamp
        address = str(log.get("address") or "").lower()
        token_hint = (
            pons_pools.get(address, (address, ""))[0]
            if venue is VenueId.PONS_V1_ROBINHOOD_4663
            else address
        )
        if venue is VenueId.PONS_V1_ROBINHOOD_4663 and is_launch:
            decoded = decode_pons_4663_log(log, pool_tokens=pons_pools)
            token_hint = decoded.token_id or address
        envelope = OpportunitySourceEnvelope(
                source_key=source_key,
                source_provider="eth_getLogs_poll",
                source_cursor=(
                    f"{parsed['block_or_slot']}:{parsed['transaction_index']}:"
                    f"{parsed['event_index']}"
                ),
                venue_id=venue,
                program_or_contract_id=address,
                transaction_id=parsed["transaction_id"],
                block_or_slot=parsed["block_or_slot"],
                transaction_index=parsed["transaction_index"],
                event_index=parsed["event_index"],
                block_hash=parsed["block_hash"],
                chain_timestamp=chain_timestamp,
                received_at=received,
                confirmation=ConfirmationState.CONFIRMED,
                payload={
                    "log": log,
                    **(
                        {
                            "pons_token_id": pons_pools[address][0],
                            "pons_pair_token": pons_pools[address][1],
                        }
                        if venue is VenueId.PONS_V1_ROBINHOOD_4663
                        and address in pons_pools else {}
                    ),
                },
                token_hint=token_hint,
        )
        submitted = runtime.submit(source_key, envelope)
        if submitted is False:
            # A rejected submission is a HOLE in this range. Advancing the scan cursor
            # past it would mark the block enumerated while its event was never queued.
            rejected_submissions += 1
        else:
            # Identify THIS range's envelopes so the drain can prove they left the queue.
            range_keys.add((source_key, envelope.source_cursor))
            pending_items += 1
        if pending_items >= persist_batch_limit:
            drain_failures += _drain_evm_poll_batch(runtime, pending_items)
            pending_items = 0
            await asyncio.sleep(0)
    drain_failures += _drain_evm_poll_batch(runtime, pending_items)
    # The queue is shared, so a batch drain can consume foreign envelopes and leave this
    # range's events queued. Prove they are gone before the caller may advance.
    drain_failures += _drain_until_range_is_clear(runtime, range_keys)
    launches = sum(int(item["launches"]) for item in by_source.values())
    last_launches = [
        str(item["last_launch_at"])
        for item in by_source.values() if item["last_launch_at"] is not None
    ]
    last_chain_events = [
        str(item["last_chain_event_at"])
        for item in by_source.values() if item["last_chain_event_at"] is not None
    ]
    # `drain` swallows a processing failure into `gapped_sources`, and a rejected
    # submission never reaches the queue at all. Either one means this range did NOT
    # persist completely, so the caller must not advance the scan cursor past it.
    # A COUNT, not a set comparison. A second failure on an already-gapped source adds
    # no new key, so the set test reported success while raw rows were never written.
    gapped_now = set(getattr(runtime, "gapped_sources", ()) or ())
    still_queued = _queued_range_items(runtime, range_keys)
    persisted_completely = (
        not rejected_submissions
        and not drain_failures
        and not still_queued
        and gapped_now <= gapped_before
    )
    return {
        "events": len(accepted),
        "launches": launches,
        "last_chain_event_at": max(last_chain_events) if last_chain_events else None,
        "last_launch_at": max(last_launches) if last_launches else None,
        "by_source": by_source,
        "persisted_completely": persisted_completely,
        "rejected_submissions": rejected_submissions,
        "drain_failures": drain_failures,
        "still_queued": still_queued,
    }


async def _evm_poll_lane(
    runtime,
    *,
    rpc_url: str,
    poll_s: float,
    stop,
    rpc_governor=None,
    abandon_fn=None,
    identities: VenueIdentityMap | None = None,
    warm_start_blocks: int = 50_000,
    chunk_blocks: int = 5_000,
    timings: _RuntimeTimingMetrics | None = None,
) -> None:
    """Forward-complete fallback with a bounded recent-history warm start."""
    source_keys = (
        "bags_robinhood_4663:ws",
        "pons_v1_4663:ws",
    )
    identities = identities or VenueIdentityMap()
    next_block = None
    recent = {
        source_key: {
            "events": 0,
            "launches": 0,
            "last_chain_event_at": None,
            "last_launch_at": None,
        }
        for source_key in source_keys
    }
    # Progress-coupled transport heartbeat. The lane used to write one only at the START
    # of a poll, so any poll longer than the 60s transport bound -- a backlog burn, or a
    # quiet range that decodes nothing -- marked a perfectly healthy lane `capture=down`.
    # This fires only from `_evm_poll_range` progress callbacks (completed header batch,
    # phase change, persisted event), never on a timer: a poll that stops advancing
    # produces no callbacks and correctly ages to stale. Throttled so a busy range does
    # not turn every event into a DB write.
    last_progress_write_s: dict[str, float] = {}

    def heartbeat_on_progress() -> None:
        now_s = time.monotonic()
        for source_key in source_keys:
            prior = last_progress_write_s.get(source_key)
            if prior is not None and now_s - prior < _EVM_PROGRESS_HEARTBEAT_MIN_S:
                continue
            _source_heartbeat(runtime, source_key)
            last_progress_write_s[source_key] = now_s

    _resolve_stored_untracked_bags_4663_gaps(runtime, identities)
    while not stop.is_set():
        poll_started_s = None
        poll_from_block = poll_to_block = None
        poll_stage = ["idle"]
        try:
            head_result, finalized_header = await _rpc_batch_call(
                rpc_governor,
                RpcPriority.CURRENT,
                rpc_url,
                [
                    ("eth_blockNumber", []),
                    ("eth_getBlockByNumber", ["finalized", False]),
                ],
            )
            head = int(head_result, 16)
            if not isinstance(finalized_header, dict) or "number" not in finalized_header:
                raise RuntimeError("4663 RPC did not return a finalized block")
            finalized_block = int(finalized_header["number"], 16)
            if finalized_block > head:
                raise RuntimeError("4663 finalized block is ahead of head")
            for venue in (
                VenueId.BAGS_ROBINHOOD_4663,
                VenueId.PONS_V1_ROBINHOOD_4663,
            ):
                source_key = f"{venue.value}:ws"
                if (
                    abandon_fn is not None
                    and frontier_gap_is_discardable(runtime.conn, source_key)
                ):
                    # Abandoning a stuck EVENT gap must NEVER move the chain scan
                    # position. Jumping to head skipped every block between the resume
                    # point and the head -- including the whole first migration range,
                    # because on a fresh store this ran before the seed existed.
                    abandon_fn(venue, real_utc_now_iso())
            if next_block is None:
                # The shared scan cursor is the resume authority. An event frontier only
                # proves where an EVENT landed, so a quiet or retired venue freezes it
                # and a restart would resume from a stale block or, with warm start at 0,
                # skip the downtime interval entirely.
                scanned_through = load_evm_scan_cursor(runtime.conn, EVM_4663_CHAIN_ID)
                if scanned_through is not None:
                    next_block = scanned_through + 1
            if next_block is None:
                prior_blocks = []
                for source_key in source_keys:
                    frontier = load_frontier(runtime.conn, source_key)
                    if frontier is None:
                        continue
                    prior = (
                        _cursor_block(frontier.resume_cursor)
                        or _cursor_block(frontier.latest_observed_cursor)
                        or _cursor_block(frontier.durable_cursor)
                    )
                    if prior is not None:
                        prior_blocks.append(prior)
                # The floor is the FINALIZED head, never the raw head. Only finalized
                # blocks are polled, so a floor above finality never becomes pollable and
                # the lane stalls forever with warm start at 0.
                finalized_head = min(head, finalized_block)
                warm_floor = (
                    max(0, finalized_head - warm_start_blocks + 1)
                    if warm_start_blocks else finalized_head
                )
                # Both venue filters are enumerated in the same bounded request. A newer
                # event frontier therefore proves the shared range was scanned even when
                # the quieter venue emitted nothing there.
                # First run with no scan cursor. Seed from the newest event frontier and
                # replay it INCLUSIVE: that block is already covered by a completed poll,
                # so re-enumerating it costs a small duplicate scan and proves the
                # boundary instead of assuming it. With no frontier at all the boundary
                # is not provable, so the warm floor stands and a gap is recorded below.
                next_block = max(prior_blocks) if prior_blocks else warm_floor
                advance_evm_scan_cursor(
                    runtime.conn,
                    chain_id=EVM_4663_CHAIN_ID,
                    through_block=max(0, next_block - 1),
                    now_iso=real_utc_now_iso(),
                    # The seed provenance names the boundary AND, when it is not
                    # provable, the interval nobody enumerated. A source gap is the wrong
                    # instrument here: a database with no frontier is a normal first run,
                    # and marking every healthy lane gapped would degrade a clean start.
                    seeded_from=(
                        "migration:event_frontier"
                        if prior_blocks
                        else f"migration:warm_floor:unverified_below={next_block}"
                    ),
                )

            # Only FINALIZED blocks are polled. Reading to head and then clamping the
            # cursor to `finalized` left the unfinalized tail read once and never again:
            # the cursor stayed behind while `next_block` moved past it.
            scan_ceiling = min(head, finalized_block)
            range_incomplete = False
            if next_block <= scan_ceiling:
                end = min(
                    scan_ceiling,
                    next_block + min(chunk_blocks, _EVM_POLL_BLOCK_LIMIT) - 1,
                )
                # The HTTP fallback is deliberately paced. Mark the start of a
                # real poll so queue wait is not mistaken for a dead source;
                # an actually stuck request still ages stale normally.
                for source_key in source_keys:
                    _source_heartbeat(runtime, source_key)
                # Bound one poll. Observed 2026-08-04: this lane stopped advancing for
                # minutes at a time with no RPC in flight, no open gap and no error --
                # the frontier froze while Pons ran ~20 minutes behind head. A restart
                # cleared it, so the lane can recover; an unbounded await cannot do it
                # by itself, and a bounded one retries the identical range next pass.
                #
                # This is NOT a hard bound: `asyncio.wait_for` can only interrupt at an
                # await point, and `_evm_poll_range` runs synchronous decode/persist work
                # through `runtime.drain()`. A blocked synchronous section still exceeds
                # this deadline. It bounds a cooperative hang only.
                #
                # Dense Pons persistence used to commit and update status once per
                # event, measuring ~7.9 blocks/s against a ~10 blocks/s chain. Poll
                # persistence is now transaction-batched; post-deploy frontier movement
                # remains the authority for whether it can burn the live backlog.
                poll_started_s = time.monotonic()
                poll_from_block, poll_to_block = next_block, end
                activity = await asyncio.wait_for(
                    _evm_poll_range(
                        runtime,
                        rpc_url=rpc_url,
                        from_block=next_block,
                        to_block=end,
                        rpc_governor=rpc_governor,
                        identities=identities,
                        canonical_through_block=finalized_block,
                        stage=poll_stage,
                        on_progress=heartbeat_on_progress,
                    ),
                    timeout=_EVM_POLL_DEADLINE_S,
                )
                for source_key, item in dict(activity.get("by_source") or {}).items():
                    if source_key not in recent:
                        continue
                    recent[source_key]["events"] = (
                        int(recent[source_key]["events"]) + int(item["events"])
                    )
                    recent[source_key]["launches"] = (
                        int(recent[source_key]["launches"]) + int(item["launches"])
                    )
                    recent[source_key]["last_chain_event_at"] = (
                        item.get("last_chain_event_at")
                        or recent[source_key]["last_chain_event_at"]
                    )
                    recent[source_key]["last_launch_at"] = (
                        item["last_launch_at"] or recent[source_key]["last_launch_at"]
                    )
                # Events for this range are committed inside `_evm_poll_range`. The scan
                # cursor commits AFTER them, in its own short transaction: a crash
                # between the two replays the range, never skips it. An empty range
                # advances the cursor too, because enumeration is the fact being stored.
                #
                # Two things must hold before the cursor may move.
                # 1. The range persisted COMPLETELY. `drain` turns a processing failure
                #    into a gapped source and a rejected submission never reaches the
                #    queue, so either one leaves a hole the cursor would hide.
                # 2. The block is FINALIZED. A reorganization can replace logs at an
                #    unfinalized height, and the lane never reads that height again.
                range_incomplete = not activity.get("persisted_completely")
                if activity.get("persisted_completely"):
                    advance_evm_scan_cursor(
                        runtime.conn,
                        chain_id=EVM_4663_CHAIN_ID,
                        through_block=end,
                        now_iso=real_utc_now_iso(),
                    )
                    next_block = end + 1
                # else: the range left a hole. `next_block` must NOT move, or the next
                # poll advances past the hole and a later complete range writes a cursor
                # that hides it. Replaying costs a duplicate scan; skipping loses blocks
                # permanently. The health block below still runs -- an incomplete range
                # is exactly when an operator needs the numbers.
            finalized_head_block = min(head, finalized_block)
            scanned_through_block = (
                load_evm_scan_cursor(runtime.conn, EVM_4663_CHAIN_ID)
                if load_evm_scan_cursor(runtime.conn, EVM_4663_CHAIN_ID) is not None
                else next_block - 1
            )
            for source_key in source_keys:
                frontier = load_frontier(runtime.conn, source_key)
                if frontier is None or not frontier.has_open_gap:
                    runtime.mark_recovered(source_key)
                    # Both 4663 venues share one bounded scan, so how far that scan
                    # trails head is the venue-independent liveness fact. An event-age
                    # bound cannot serve here: Bags is genuinely quiet for hours, so it
                    # graded `healthy` while the shared scan sat ~11,700 blocks (~20
                    # minutes) behind head and every 4663 signal arrived far too late to
                    # act on. The writer declares its own bound, as with
                    # `transport_stale_after_s`; the projection only reads it.
                    existing_detail = _stored_detail(runtime.conn, source_key)
                    detail = {
                        "capture_state": "listening",
                        "recent_events": recent[source_key]["events"],
                        "recent_launches": recent[source_key]["launches"],
                        "last_launch_at": recent[source_key]["last_launch_at"],
                        "warm_start_blocks": warm_start_blocks,
                        "transport_stale_after_s": _EVM_TRANSPORT_STALE_AFTER_S,
                        "head_block": head,
                        # The lane deliberately stops at finality, so "behind" must be
                        # measured against the FINALIZED head. Against the raw head it
                        # reads one finality delay behind forever -- ~9,000 blocks on
                        # this chain -- and grades a caught-up lane degraded for good.
                        "finalized_head_block": finalized_head_block,
                        # The DURABLE cursor, not the in-memory loop variable. The two
                        # diverge exactly when a range fails to persist, which is when
                        # an operator most needs the real number.
                        "scanned_through_block": scanned_through_block,
                        "blocks_behind_head": max(
                            0, finalized_head_block - scanned_through_block
                        ),
                        "blocks_behind_raw_head": max(0, head - scanned_through_block),
                        "max_blocks_behind_head": _EVM_MAX_BLOCKS_BEHIND_HEAD,
                        # A success clears the streak but never the history. The failure
                        # rate is the thing an operator needs and it must not be erasable
                        # by one good poll.
                        "consecutive_poll_failures": 0,
                        "poll_failures_total": int(
                            existing_detail.get("poll_failures_total") or 0),
                    }
                    for carried in ("last_poll_failure", "last_poll_failure_at"):
                        if existing_detail.get(carried):
                            detail[carried] = existing_detail[carried]
                    if recent[source_key]["last_chain_event_at"] is not None:
                        detail["last_chain_event_at"] = recent[source_key][
                            "last_chain_event_at"
                        ]
                    if source_key == "pons_v1_4663:ws":
                        detail["expected_event_max_age_s"] = 300
                        if (
                            "last_chain_event_at" not in detail
                            and existing_detail.get("last_chain_event_at")
                        ):
                            detail["last_chain_event_at"] = existing_detail[
                                "last_chain_event_at"
                            ]
                        detail["event_expectation_started_at"] = existing_detail.get(
                            "event_expectation_started_at",
                            runtime.now_fn() if hasattr(runtime, "now_fn") else real_utc_now_iso(),
                        )
                    runtime.heartbeat(source_key, detail=detail)
                else:
                    _source_heartbeat(runtime, source_key)
        except asyncio.CancelledError:
            if range_incomplete:
                # Retry the SAME range on the next cycle. Health above already ran: an
                # incomplete range is exactly when the operator needs the numbers.
                break
            raise
        except Exception as exc:
            # `exc` was bound and never used, so every failure here was silent and a
            # frozen frontier produced an empty worker log. A TimeoutError also carries
            # no message, so "4663 poll failed: TimeoutError:" identified nothing -- the
            # handler has to supply the range and how long it hung, or the log records
            # only that something went wrong.
            elapsed_s = (
                round(time.monotonic() - poll_started_s, 1)
                if poll_started_s is not None else None
            )
            print(
                "4663 poll failed: "
                f"{type(exc).__name__}: {str(exc)[:300] or '(no message)'} "
                f"blocks={poll_from_block}..{poll_to_block} "
                f"stage={poll_stage[0]} "
                f"elapsed_s={elapsed_s} deadline_s={_EVM_POLL_DEADLINE_S}",
                file=sys.stderr,
                flush=True,
            )
            # NOT `_report_lane_failure`: that opens a frontier gap, which asserts we
            # lost data at a cursor and must recover. A bounded poll retries the exact
            # same range next pass, so nothing is lost -- the scan only falls behind, and
            # `blocks_behind_head` is what measures that. Opening a gap here would also
            # never resolve (the success path only marks recovered when no gap is open),
            # so `source_gapped` would latch on and censor every 4663 outcome forever.
            #
            # The real defect was durability: a bare degraded heartbeat is overwritten by
            # the next good poll, so a lane failing dozens of times a day left no record.
            # These counters survive the next success and make the failure rate auditable.
            failure = (
                f"{type(exc).__name__}: {str(exc)[:160] or '(no message)'} "
                f"blocks={poll_from_block}..{poll_to_block} stage={poll_stage[0]} "
                f"elapsed_s={elapsed_s}"
            )
            for source_key in source_keys:
                stored = _stored_detail(runtime.conn, source_key)
                runtime.heartbeat(
                    source_key,
                    status="degraded",
                    blocker_code="SOURCE_FAILURE",
                    detail={
                        **stored,
                        "last_poll_failure": failure,
                        "last_poll_failure_at": real_utc_now_iso(),
                        "poll_failures_total": int(
                            stored.get("poll_failures_total") or 0) + 1,
                        "consecutive_poll_failures": int(
                            stored.get("consecutive_poll_failures") or 0) + 1,
                    },
                )
        if timings is not None and poll_started_s is not None:
            timings.observe(
                "evm_lane",
                time.monotonic() - poll_started_s,
                limit_s=60.0,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_s)
        except asyncio.TimeoutError:
            pass


def _unique_confirmation_signatures(rows, *, limit: int = 256) -> list[str]:
    return list(dict.fromkeys(row["transaction_id"] for row in rows))[:limit]


def _selected_pending_confirmations(
    pending: dict[str, set[str]],
    *,
    limit: int = 256,
    priority_transaction_ids: tuple[str, ...] = (),
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Bound per-cycle DB work by signature, not the full pending backlog."""
    ordered_ids: list[str] = []
    selected_ids: set[str] = set()
    for transaction_id in (*priority_transaction_ids, *pending.keys()):
        if transaction_id not in pending or transaction_id in selected_ids:
            continue
        ordered_ids.append(transaction_id)
        selected_ids.add(transaction_id)
        if len(ordered_ids) >= limit:
            break
    return tuple(
        (transaction_id, tuple(pending[transaction_id]))
        for transaction_id in ordered_ids
    )


def _forward_confirmation_priorities(conn, *, limit: int = 64) -> tuple[str, ...]:
    """Return unresolved signatures that gate immutable forward result paths."""
    return tuple(
        row["transaction_id"]
        for row in conn.execute(
            "SELECT DISTINCT event.transaction_id FROM op_forward_alerts alert "
            "JOIN op_state_snapshots snapshot USING(snapshot_id) "
            "JOIN op_canonical_events event "
            "ON event.canonical_event_id=snapshot.observed_event_id "
            "WHERE event.confirmation='tentative' AND NOT EXISTS ("
            "SELECT 1 FROM op_canonical_events correction "
            "WHERE correction.correction_of=event.canonical_event_id "
            "AND correction.confirmation IN ('finalized','orphaned')) "
            "ORDER BY alert.observed_at,event.transaction_id LIMIT ?",
            (limit,),
        ).fetchall()
    )


def _track_pending_confirmation(pending, envelope, result) -> None:
    """Track durability independently of whether the event can update Radar state."""
    if (
        result.canonical_event_id
        and result.canonical_inserted
        and envelope.confirmation is ConfirmationState.TENTATIVE
    ):
        pending.setdefault(envelope.transaction_id, set()).add(
            result.canonical_event_id
        )


def _solana_confirmation_target(
    status,
    *,
    received_at: str,
    now_iso: str,
    orphan_after_s: float,
) -> ConfirmationState | None:
    """Resolve terminal Solana status without leaking missing signatures forever."""
    if isinstance(status, dict) and status:
        if status.get("err") is not None:
            return ConfirmationState.ORPHANED
        if status.get("confirmationStatus") == "finalized":
            return ConfirmationState.FINALIZED
        return None
    age_s = (_parse_iso(now_iso) - _parse_iso(received_at)).total_seconds()
    if age_s >= orphan_after_s:
        return ConfirmationState.ORPHANED
    return None


def _mark_confirmation_recovered(runtime, source_keys: tuple[str, ...]) -> None:
    for source_key in source_keys:
        frontier = load_frontier(runtime.conn, source_key)
        if frontier is not None and frontier.has_open_gap:
            continue
        row = runtime.conn.execute(
            "SELECT blocker_code,detail_json FROM op_runtime_status WHERE source_key=?",
            (source_key,),
        ).fetchone()
        if row is None or row["blocker_code"] != "CONFIRMATION_FAILURE":
            continue
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        runtime.mark_recovered(source_key, detail=detail)


def _resolve_terminal_orphan_gaps(
    conn,
    runtime,
    *,
    source_keys: set[str],
    observed_at: str,
) -> None:
    """Close only orphan gaps whose canonical state was rebuilt successfully."""
    terminal_details = {
        "terminal_solana_confirmation_orphan",
        "terminal_evm_confirmation_orphan",
    }
    for source_key in source_keys:
        current = load_frontier(conn, source_key)
        if current is None or not current.has_open_gap:
            continue
        orphan_gap_ids = tuple(
            row["gap_id"]
            for row in list_open_gaps(conn, source_key)
            if row["detail"] in terminal_details
        )
        target_cursor = current.latest_observed_cursor or current.durable_cursor
        if not orphan_gap_ids or target_cursor is None:
            continue
        current = apply_frontier_batch(
            conn,
            FrontierBatch(
                source_key=source_key,
                expected_version=current.version,
                previous_durable_cursor=current.durable_cursor,
                newest_observed_cursor=target_cursor,
                contiguous_durable_cursor=target_cursor,
                resume_cursor=current.resume_cursor,
                reached_previous_frontier=True,
                enumeration_complete=True,
                decode_complete=True,
                observed_at=observed_at,
                resolved_gap_ids=orphan_gap_ids,
            ),
        )
        if current.has_open_gap:
            _source_heartbeat(runtime, source_key)
        else:
            runtime.mark_recovered(source_key)


async def _confirmation_lane(
    conn,
    pending: dict[str, set[str]],
    *,
    runtime,
    solana_rpc_url: str,
    robinhood_rpc_url: str,
    stop,
    poll_s: float = 5.0,
    rpc_governor=None,
    evm_rpc_governor=None,
    solana_orphan_after_s: float = 120.0,
    rebuild_fn=None,
):
    """Reconcile fast tentative observations without delaying the Radar trigger."""
    while not stop.is_set():
        rebuild_needed = False
        orphan_source_keys: set[str] = set()
        rows = []
        priority_transaction_ids = _forward_confirmation_priorities(conn)
        for _transaction_id, event_ids in _selected_pending_confirmations(
            pending,
            priority_transaction_ids=priority_transaction_ids,
        ):
            for event_id in tuple(event_ids):
                row = conn.execute(
                    "SELECT canonical_event_id,network_id,venue_id,transaction_id,block_or_slot,"
                    "block_hash,received_at "
                    "FROM op_canonical_events WHERE canonical_event_id=?",
                    (event_id,),
                ).fetchone()
                if row is not None:
                    rows.append(row)
        sol_rows = [row for row in rows if row["network_id"] == "solana"]
        if sol_rows and solana_rpc_url:
            solana_source_keys = tuple(sorted({
                f"{row['venue_id']}:ws" for row in sol_rows
            }))
            try:
                signatures = _unique_confirmation_signatures(sol_rows)
                result = await _rpc_call(
                    rpc_governor, RpcPriority.CONFIRMATION, solana_rpc_url,
                    "getSignatureStatuses",
                    [signatures, {"searchTransactionHistory": True}],
                )
                statuses = (result or {}).get("value") or []
                by_signature = dict(zip(signatures, statuses))
                reconciled_at = real_utc_now_iso()
                unresolved_signatures = set()
                resolved_rows = []
                with transaction(conn):
                    for row in sol_rows:
                        status = by_signature.get(row["transaction_id"])
                        target = _solana_confirmation_target(
                            status,
                            received_at=row["received_at"],
                            now_iso=reconciled_at,
                            orphan_after_s=solana_orphan_after_s,
                        )
                        if target is None:
                            unresolved_signatures.add(row["transaction_id"])
                            continue
                        reconcile_confirmation(
                            conn,
                            canonical_event_id=row["canonical_event_id"],
                            confirmation=target,
                            reconciled_at=reconciled_at,
                        )
                        resolved_rows.append((row, target))
                        if target is ConfirmationState.ORPHANED:
                            source_key = f"{row['venue_id']}:ws"
                            record_source_failure(
                                conn,
                                source_key=source_key,
                                observed_at=reconciled_at,
                                detail="terminal_solana_confirmation_orphan",
                            )
                            orphan_source_keys.add(source_key)
                for row, target in resolved_rows:
                    pending.get(row["transaction_id"], set()).discard(row["canonical_event_id"])
                    if target is ConfirmationState.ORPHANED:
                        source_key = f"{row['venue_id']}:ws"
                        runtime.heartbeat(
                            source_key, status="degraded", blocker_code="EVENT_GAP"
                        )
                        rebuild_needed = True
                        orphan_source_keys.add(source_key)
                # Do not let a recently confirmed-but-not-finalized signature
                # pin the head of the bounded queue and starve later arrivals.
                for signature in unresolved_signatures:
                    if signature in pending:
                        pending[signature] = pending.pop(signature)
                _mark_confirmation_recovered(runtime, solana_source_keys)
            except asyncio.CancelledError:
                raise
            except Exception:
                for source_key in solana_source_keys:
                    runtime.heartbeat(
                        source_key,
                        status="degraded",
                        blocker_code="CONFIRMATION_FAILURE",
                    )
        evm_rows = [row for row in rows if row["network_id"] == "4663"]
        if evm_rows and robinhood_rpc_url:
            try:
                evm_governor = evm_rpc_governor or rpc_governor
                finalized_block = await _rpc_call(
                    evm_governor, RpcPriority.CONFIRMATION, robinhood_rpc_url,
                    "eth_getBlockByNumber",
                    ["finalized", False],
                )
                finalized_height = _finalized_evm_height(finalized_block)
                if finalized_height is None:
                    raise RuntimeError("finalized EVM block unavailable")
                for row in evm_rows[:64]:
                    if row["block_or_slot"] > finalized_height:
                        continue
                    receipt = await _rpc_call(
                        evm_governor, RpcPriority.CONFIRMATION, robinhood_rpc_url,
                        "eth_getTransactionReceipt",
                        [row["transaction_id"]],
                    )
                    target = _evm_receipt_confirmation(row, receipt)
                    reconciled_at = real_utc_now_iso()
                    reconcile_confirmation(
                        conn,
                        canonical_event_id=row["canonical_event_id"],
                        confirmation=target,
                        reconciled_at=reconciled_at,
                    )
                    pending.get(row["transaction_id"], set()).discard(row["canonical_event_id"])
                    if target is ConfirmationState.ORPHANED:
                        source_key = f"{row['venue_id']}:ws"
                        record_source_failure(
                            conn,
                            source_key=source_key,
                            observed_at=reconciled_at,
                            detail="terminal_evm_confirmation_orphan",
                        )
                        runtime.heartbeat(
                            source_key, status="degraded", blocker_code="EVENT_GAP"
                        )
                        rebuild_needed = True
                        orphan_source_keys.add(source_key)
                _mark_confirmation_recovered(
                    runtime,
                    ("bags_robinhood_4663:ws", "pons_v1_4663:ws"),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                runtime.heartbeat(
                    "bags_robinhood_4663:ws",
                    status="degraded",
                    blocker_code="CONFIRMATION_FAILURE",
                )
                runtime.heartbeat(
                    "pons_v1_4663:ws",
                    status="degraded",
                    blocker_code="CONFIRMATION_FAILURE",
                )
        if rebuild_needed and rebuild_fn is not None:
            rebuild_fn()
            _resolve_terminal_orphan_gaps(
                conn,
                runtime,
                source_keys=orphan_source_keys,
                observed_at=real_utc_now_iso(),
            )
        for txid in [key for key, values in pending.items() if not values]:
            pending.pop(txid, None)
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=0.05 if len(pending) > 512 else poll_s,
            )
        except asyncio.TimeoutError:
            pass


def _evm_receipt_confirmation(row, receipt) -> ConfirmationState:
    if not isinstance(receipt, dict):
        return ConfirmationState.ORPHANED
    stored_hash = str(row["block_hash"] or "").lower()
    receipt_hash = str(receipt.get("blockHash") or "").lower()
    try:
        succeeded = int(str(receipt.get("status") or "0"), 16) == 1
    except ValueError:
        succeeded = False
    if not stored_hash or receipt_hash != stored_hash or not succeeded:
        return ConfirmationState.ORPHANED
    return ConfirmationState.FINALIZED


def _finalized_evm_height(block) -> int | None:
    if not isinstance(block, dict) or not block.get("number"):
        return None
    try:
        return int(str(block["number"]), 16)
    except (TypeError, ValueError):
        return None


async def run(settings) -> None:
    require_capture_enabled(settings)
    worker_started_at = real_utc_now_iso()
    timings = _RuntimeTimingMetrics()
    lock_path = Path(settings.db_path).with_suffix(".worker.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise RuntimeError(f"opportunity worker already owns {lock_path}") from exc
    conn = connect(settings.db_path)
    if settings.paper_adapter_enabled:
        _activate_paper_lifecycles(
            conn,
            adaptive_v3_enabled=settings.adaptive_v3_enabled,
            now_iso=worker_started_at,
            realistic_v3_authorised=activation_authorised(),
        )
    insights_conn = connect_insights(settings.insight_db_path)
    rpc_governor = OpportunityRpcGovernor(
        concurrency=settings.rpc_concurrency,
        initial_rps=settings.rpc_initial_rps,
        min_rps=settings.rpc_min_rps,
        max_rps=settings.rpc_max_rps,
    )
    recovery_rpc_governor = OpportunityRpcGovernor(
        concurrency=1,
        initial_rps=settings.solana_recovery_rps,
        min_rps=max(0.25, settings.solana_recovery_rps / 4.0),
        max_rps=settings.solana_recovery_rps,
    )
    robinhood_rpc_governor = OpportunityRpcGovernor(
        concurrency=1,
        initial_rps=settings.robinhood_rpc_rps,
        min_rps=settings.robinhood_rpc_rps,
        max_rps=settings.robinhood_rpc_rps,
    )
    identities = VenueIdentityMap()
    rpc_4663 = (
        lambda method, params: robinhood_rpc_governor.call_sync(
            RpcPriority.QUOTE,
            json_rpc_http,
            settings.robinhood_rpc_url,
            method,
            params,
        )
        if settings.robinhood_rpc_url
        else None
    )
    bags_4663_state_enricher = (
        make_bags_4663_state_enricher(rpc_4663)
        if rpc_4663 is not None
        else None
    )
    solana_probe = OpportunitySolanaProbe(
        min_call_interval_s=float(
            os.getenv("MEMECOIN_OPPORTUNITY_JUPITER_MIN_CALL_S", "2")
        ),
        cache_ttl_s=float(
            os.getenv("MEMECOIN_OPPORTUNITY_JUPITER_CACHE_TTL_S", "5")
        ),
        queue_max=int(os.getenv("MEMECOIN_OPPORTUNITY_JUPITER_QUEUE_MAX", "1")),
        max_queue_age_ms=settings.quote_max_queue_age_ms,
        max_pair_age_ms=settings.quote_max_pair_age_ms,
        max_completion_age_ms=settings.quote_max_completion_age_ms,
        cache_max=settings.quote_cache_max,
        completion_max=settings.quote_completion_max,
    )
    bags_4663_provider = (
        make_bags_4663_quote_provider(rpc_4663)
        if settings.robinhood_rpc_url else None
    )
    pons_4663_provider = (
        make_pons_4663_quote_provider(
            rpc_4663,
            quoter=settings.robinhood_v3_quoter,
        )
        if settings.robinhood_rpc_url and settings.robinhood_v3_quoter else None
    )
    evm_probe = OpportunityEvmProbe(
        {
            venue: provider
            for venue, provider in (
                (VenueId.BAGS_ROBINHOOD_4663, bags_4663_provider),
                (VenueId.PONS_V1_ROBINHOOD_4663, pons_4663_provider),
            )
            if provider is not None
        },
        queue_max=int(os.getenv("MEMECOIN_OPPORTUNITY_EVM_QUOTE_QUEUE_MAX", "16")),
        completion_max=settings.quote_completion_max,
    )

    def remote_probe(state, quote):
        if state.venue_id is VenueId.PUMPFUN_SOLANA:
            return solana_probe(state, quote)
        if state.venue_id in {
            VenueId.BAGS_ROBINHOOD_4663,
            VenueId.PONS_V1_ROBINHOOD_4663,
        }:
            return evm_probe(state, quote)
        return RemoteRoundTripProbe(
            False, False, "unavailable", 0, "REMOTE_SIMULATION_UNAVAILABLE"
        )
    price_feed = NativeUsdPriceFeed(
        refresh_every_s=float(os.getenv("MEMECOIN_OPPORTUNITY_NATIVE_PRICE_REFRESH_S", "30"))
    )
    market_inputs, _market_blockers = price_feed.load(
        os.environ,
        now_iso=real_utc_now_iso(),
        monotonic_s=time.monotonic(),
    )
    engine = OpportunityEngine(
        conn,
        quote_usd_price_micros={venue: item.usd_price_micros for venue, item in market_inputs.items()},
        quote_decimals={venue: item.quote_decimals for venue, item in market_inputs.items()},
        fee_bps={venue: item.fee_bps for venue, item in market_inputs.items()},
        entry_latency_ms={
            VenueId.PUMPFUN_SOLANA: int(os.getenv("MEMECOIN_OPPORTUNITY_SOL_ENTRY_P90_MS", "500")),
            VenueId.BAGS_ROBINHOOD_4663: int(os.getenv("MEMECOIN_OPPORTUNITY_4663_ENTRY_P90_MS", "300")),
            VenueId.PONS_V1_ROBINHOOD_4663: int(os.getenv("MEMECOIN_OPPORTUNITY_4663_ENTRY_P90_MS", "300")),
        },
        network_cost_quote_atomic={
            venue: item.network_cost_quote_atomic for venue, item in market_inputs.items()
        },
        quote_input_as_of={venue: item.price_as_of for venue, item in market_inputs.items()},
        remote_probe=remote_probe,
        remote_completion_max_age_ms=settings.quote_max_completion_age_ms,
        forward_arm_min_launched_at=worker_started_at,
        paper_adapter_enabled=settings.paper_adapter_enabled,
        adaptive_v3_enabled=settings.adaptive_v3_enabled,
        paper_adapter_expiry_s=settings.paper_adapter_expiry_s,
    )
    paper_quote_fn = _paper_quote_fn(solana_probe, evm_probe, engine)
    paper_sampler = PaperSampler.restore(conn, now_iso=worker_started_at)
    paper_exit_quote_fn = _paper_exit_quote_dispatch_fn(
        solana_probe,
        evm_probe,
        sampler=paper_sampler,
    )
    restore_pipeline(conn, engine, identities)
    pending_confirmations = load_pending_confirmations(conn)
    source_epoch_started_at: dict[str, str] = {}

    def process(envelope):
        # Apply the venue authority before the worker writes an event.
        if not is_active_capture_venue(envelope.venue_id):
            return
        epoch_started_at = source_epoch_started_at.get(envelope.source_key)
        if epoch_started_at is not None and envelope.received_at <= epoch_started_at:
            return
        if envelope.venue_id is VenueId.BAGS_ROBINHOOD_4663:
            log = envelope.payload.get("log")
            if isinstance(log, dict) and is_untracked_bags_4663_curve_log(
                log, identities.token_by_4663_contract
            ):
                return
        if envelope.confirmation is ConfirmationState.ORPHANED:
            prior_rows = conn.execute(
                "SELECT canonical_event_id FROM op_canonical_events WHERE transaction_id=? "
                "AND block_or_slot=? AND COALESCE(transaction_index,-1)=COALESCE(?,-1) "
                "AND event_index=? AND confirmation='tentative' AND event_kind!='correction'",
                (
                    envelope.transaction_id,
                    envelope.block_or_slot,
                    envelope.transaction_index,
                    envelope.event_index,
                ),
            ).fetchall()
            for prior in prior_rows:
                reconcile_confirmation(
                    conn,
                    canonical_event_id=prior["canonical_event_id"],
                    confirmation=ConfirmationState.ORPHANED,
                    reconciled_at=envelope.received_at,
                )
            runtime.heartbeat(envelope.source_key, status="degraded", blocker_code="EVENT_GAP")
            record_source_failure(
                conn,
                source_key=envelope.source_key,
                observed_at=envelope.received_at,
                detail="orphaned event requires source recovery",
            )
            restore_pipeline(conn, engine, identities)
            return
        result = process_envelope(
            conn,
            envelope,
            identities=identities,
            decoded_enricher=(
                bags_4663_state_enricher
                if envelope.venue_id is VenueId.BAGS_ROBINHOOD_4663
                else None
            ),
            capture_window_s=settings.capture_window_s,
            candidate_capture_window_s=settings.candidate_capture_window_s,
        )
        if result.blocker:
            record_processed_cursor(
                conn,
                source_key=envelope.source_key,
                source_cursor=envelope.source_cursor,
                observed_at=envelope.received_at,
                decode_complete=False,
                detail=result.blocker,
            )
            runtime.heartbeat(
                envelope.source_key,
                status="degraded",
                blocker_code=result.blocker,
            )
            return
        _track_pending_confirmation(pending_confirmations, envelope, result)
        if not (
            result.canonical_event_id
            and result.canonical_inserted
            and envelope.confirmation is not ConfirmationState.ORPHANED
        ):
            # Duplicate and post-window events still advance the durable source
            # frontier. They are intentionally absent from the hot ledger.
            record_processed_cursor(
                conn,
                source_key=envelope.source_key,
                source_cursor=envelope.source_cursor,
                observed_at=envelope.received_at,
                decode_complete=True,
            )
            return
        if (
            result.canonical_event_id
            and result.canonical_inserted
            and envelope.confirmation is not ConfirmationState.ORPHANED
        ):
            launch_id = conn.execute(
                "SELECT launch_id FROM op_canonical_events WHERE canonical_event_id=?",
                (result.canonical_event_id,),
            ).fetchone()[0]
            if not _engine_state_available(engine, result.decoded, launch_id):
                record_processed_cursor(
                    conn,
                    source_key=envelope.source_key,
                    source_cursor=envelope.source_cursor,
                    observed_at=envelope.received_at,
                    decode_complete=True,
                )
                return
            try:
                engine.observe_decoded(
                    decoded=result.decoded,
                    canonical_event_id=result.canonical_event_id,
                    confirmation=envelope.confirmation,
                    observed_at=envelope.received_at,
                    source_gap_open=(
                        runtime.source_gapped(envelope.source_key)
                        or envelope.source_provider.endswith("_recovery")
                    ),
                )
            except OutOfOrderObservation:
                record_processed_cursor(
                    conn,
                    source_key=envelope.source_key,
                    source_cursor=envelope.source_cursor,
                    observed_at=envelope.received_at,
                    decode_complete=True,
                )
                return
            record_processed_cursor(
                conn,
                source_key=envelope.source_key,
                source_cursor=envelope.source_cursor,
                observed_at=envelope.received_at,
                decode_complete=True,
            )

    runtime = OpportunityCaptureRuntime(
        conn, settings=settings, process_fn=process, now_fn=real_utc_now_iso
    )

    def abandon_source_gap(venue: VenueId, observed_at: str) -> None:
        source_key = f"{venue.value}:ws"
        if not frontier_gap_is_discardable(conn, source_key):
            return
        source_epoch_started_at[source_key] = observed_at
        launch_ids = engine.quarantine_venue(venue, observed_at=observed_at)
        quarantined_event_ids = {
            str(row[0])
            for launch_id in launch_ids
            for row in conn.execute(
                "SELECT canonical_event_id FROM op_canonical_events WHERE launch_id=?",
                (launch_id,),
            ).fetchall()
        }
        for transaction_id, event_ids in tuple(pending_confirmations.items()):
            event_ids.difference_update(quarantined_event_ids)
            if not event_ids:
                pending_confirmations.pop(transaction_id, None)
        discard_launch_episodes(
            conn,
            insights_conn,
            launch_ids=launch_ids,
            observed_at=observed_at,
        )
        rebase_frontier_after_discard(
            conn,
            source_key,
            observed_at=observed_at,
            detail=(
                "bounded recovery exhausted; affected launch states quarantined"
            ),
        )
        runtime.mark_recovered(
            source_key,
            detail={
                "capture_epoch_started_at": observed_at,
                "discarded_launches": len(launch_ids),
                "recovery_mode": "current_epoch_rebased",
            },
        )
    def refresh_market_inputs() -> None:
        inputs, blockers = price_feed.load(
            os.environ,
            now_iso=real_utc_now_iso(),
            monotonic_s=time.monotonic(),
        )
        engine.replace_market_inputs(inputs)
        for venue in active_capture_venues():
            blocker = blockers.get(venue)
            runtime.heartbeat(
                f"{venue.value}:pricing",
                status="degraded" if blocker else "healthy",
                blocker_code=blocker,
            )

    refresh_market_inputs()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    tasks = []
    solana_block_cache = {}
    pump_ws = _solana_ws_transport(settings)
    def start_source_tasks():
        started = []
        if pump_ws:
            pump_recovery_url, pump_recovery_governor, pump_indexed = (
                _pump_recovery_transport(
                    settings, rpc_governor, recovery_rpc_governor
                )
            )
            started.append(
                asyncio.create_task(
                    _supervise_lane(lambda: _solana_multi_lane(
                        runtime,
                        ws_url=pump_ws,
                        rpc_url=settings.solana_rpc_url,
                        recovery_rpc_url=pump_recovery_url,
                        recovery_max_pages=settings.recovery_max_pages,
                        stop=stop,
                        block_cache=solana_block_cache,
                        rpc_governor=rpc_governor,
                        recovery_rpc_governor=pump_recovery_governor,
                        indexed_recovery=pump_indexed,
                        bags_pool_mints={},
                        include_pump=True,
                        abandon_fn=abandon_source_gap,
                        timings=timings,
                    ), label="pumpfun_solana:ws", stop=stop)
                )
            )
        if settings.robinhood_ws_url and settings.robinhood_rpc_url:
            started.append(
                asyncio.create_task(
                    _supervise_lane(lambda: _evm_lane(
                        runtime,
                        ws_url=settings.robinhood_ws_url,
                        rpc_url=settings.robinhood_rpc_url,
                        recovery_max_pages=settings.recovery_max_pages,
                        stop=stop,
                        rpc_governor=robinhood_rpc_governor,
                        abandon_fn=abandon_source_gap,
                        identities=identities,
                    ), label="evm:ws", stop=stop)
                )
            )
        elif settings.robinhood_rpc_url:
            started.append(
                asyncio.create_task(
                    _supervise_lane(lambda: _evm_poll_lane(
                        runtime,
                        rpc_url=settings.robinhood_rpc_url,
                        poll_s=settings.robinhood_poll_s,
                        stop=stop,
                        rpc_governor=robinhood_rpc_governor,
                        abandon_fn=abandon_source_gap,
                        identities=identities,
                        warm_start_blocks=settings.robinhood_warm_start_blocks,
                        chunk_blocks=settings.robinhood_poll_chunk_blocks,
                        timings=timings,
                    ), label="evm:poll", stop=stop)
                )
            )
        return started

    source_tasks = start_source_tasks()
    solana_confirmation_url, solana_confirmation_governor = (
        _solana_confirmation_transport(
            settings, rpc_governor, recovery_rpc_governor
        )
    )
    if settings.solana_rpc_url or settings.robinhood_rpc_url:
        tasks.append(
            asyncio.create_task(
                _confirmation_lane(
                    conn,
                    pending_confirmations,
                    runtime=runtime,
                    solana_rpc_url=solana_confirmation_url,
                    robinhood_rpc_url=settings.robinhood_rpc_url,
                    stop=stop,
                    rpc_governor=solana_confirmation_governor,
                    evm_rpc_governor=robinhood_rpc_governor,
                    solana_orphan_after_s=settings.solana_orphan_after_s,
                    rebuild_fn=lambda: restore_pipeline(conn, engine, identities),
                )
            )
        )
    tasks.extend(source_tasks)
    next_outcome_sweep = loop.time()
    next_paper_adapter_sweep = loop.time()
    next_maintenance = loop.time()
    next_runtime_status = loop.time()
    ingestion_paused = False
    try:
        while not stop.is_set():
            main_loop_started_s = time.monotonic()
            runtime.drain(max_items=512)
            quote_started_s = time.monotonic()
            try:
                _drain_solana_quote_completions(
                    solana_probe,
                    engine,
                    observed_at=real_utc_now_iso(),
                )
                _drain_evm_quote_completions(
                    evm_probe,
                    engine,
                    observed_at=real_utc_now_iso(),
                )
            finally:
                timings.observe(
                    "quote_lane",
                    time.monotonic() - quote_started_s,
                    limit_s=0.1,
                )
            if settings.paper_adapter_enabled and loop.time() >= next_paper_adapter_sweep:
                now = real_utc_now_iso()
                paper_started_s = time.monotonic()
                try:
                    _drive_paper_adapter_safely(
                        conn,
                        quote_fn=paper_quote_fn,
                        now_iso=now,
                    )
                    _drive_paper_lifecycles_safely(
                        conn,
                        quote_fn=paper_exit_quote_fn,
                        observation_fn=lambda entry, now_iso: (
                            _adaptive_observation_facts(
                                engine, entry, now_iso=now_iso,
                            )
                        ),
                        now_iso=now,
                        adaptive_v3_enabled=settings.adaptive_v3_enabled,
                        sampler=paper_sampler,
                        solana_probe=solana_probe,
                        evm_probe=evm_probe,
                    )
                finally:
                    timings.observe(
                        "paper_lifecycle",
                        time.monotonic() - paper_started_s,
                        limit_s=0.5,
                    )
                next_paper_adapter_sweep = loop.time() + 0.1
            if loop.time() >= next_outcome_sweep:
                refresh_market_inputs()
                source_health = {
                    VenueId.PUMPFUN_SOLANA: (
                        not runtime.source_gapped("pumpfun_solana:ws")
                        and VenueId.PUMPFUN_SOLANA in engine.quote_usd_price_micros
                    ),
                    VenueId.BAGS_ROBINHOOD_4663: (
                        not runtime.source_gapped("bags_robinhood_4663:ws")
                        and VenueId.BAGS_ROBINHOOD_4663 in engine.quote_usd_price_micros
                    ),
                    VenueId.PONS_V1_ROBINHOOD_4663: (
                        not runtime.source_gapped("pons_v1_4663:ws")
                        and VenueId.PONS_V1_ROBINHOOD_4663 in engine.quote_usd_price_micros
                    ),
                }
                now = real_utc_now_iso()
                engine.censor_gapped_outcomes(
                    now,
                    source_healthy_by_venue=source_health,
                )
                engine.finalize_due_outcomes(
                    now,
                    source_healthy_by_venue=source_health,
                )
                next_outcome_sweep = loop.time() + 1.0
            if (
                loop.time() >= next_maintenance
                and len(runtime.queue) < max(1, settings.queue_max // 4)
            ):
                maintenance_started_s = time.monotonic()
                try:
                    storage = maintain_storage(
                        conn,
                        insights_conn,
                        now_iso=real_utc_now_iso(),
                        hot_retention_s=settings.hot_retention_s,
                        emergency_retention_s=settings.hot_emergency_retention_s,
                        insight_retention_s=settings.insight_retention_s,
                        alert_retention_s=settings.alert_retention_s,
                        hot_soft_cap_bytes=settings.hot_soft_cap_bytes,
                        hot_hard_cap_bytes=settings.hot_hard_cap_bytes,
                        insight_soft_cap_bytes=settings.insight_soft_cap_bytes,
                        insight_hard_cap_bytes=settings.insight_hard_cap_bytes,
                    )
                finally:
                    timings.observe(
                        "maintenance",
                        time.monotonic() - maintenance_started_s,
                        limit_s=5.0,
                    )
                if storage.deleted_launches:
                    restore_pipeline(conn, engine, identities)
                now = real_utc_now_iso()
                upsert_runtime_status(
                    conn,
                    source_key="opportunity:storage",
                    status=storage.status,
                    blocker_code=storage.blocker_code,
                    heartbeat_at=now,
                    queue_depth=len(runtime.queue),
                    detail=storage.detail(),
                )
                rpc_metrics = _combined_rpc_metrics(
                    rpc_governor.metrics(),
                    recovery_rpc_governor.metrics(),
                    robinhood_rpc_governor.metrics(),
                )
                rpc_backlogged = int(rpc_metrics["queue_depth"]) > 64
                upsert_runtime_status(
                    conn,
                    source_key="opportunity:rpc",
                    status="degraded" if rpc_backlogged else "healthy",
                    blocker_code="RPC_BACKLOG" if rpc_backlogged else None,
                    heartbeat_at=now,
                    queue_depth=int(rpc_metrics["queue_depth"]),
                    detail=rpc_metrics,
                )
                solana_probe_metrics = solana_probe.metrics()
                evm_probe_metrics = evm_probe.metrics()
                quote_path_rows = _quote_path_status_rows(
                    solana_metrics=solana_probe_metrics,
                    evm_metrics=evm_probe_metrics,
                    now_iso=now,
                )
                _publish_quote_path_status_rows(
                    conn,
                    quote_path_rows,
                    now_iso=now,
                )
                pump_quote_status = quote_path_rows[
                    VenueId.PUMPFUN_SOLANA.value
                ]
                upsert_runtime_status(
                    conn,
                    source_key="opportunity:solana_quote",
                    status=str(pump_quote_status["status"]),
                    blocker_code=pump_quote_status.get("blocker_code"),
                    heartbeat_at=now,
                    queue_depth=int(pump_quote_status["queue_depth"]),
                    detail=solana_probe_metrics,
                )
                evm_quote_failures = [
                    row
                    for venue, row in quote_path_rows.items()
                    if venue != VenueId.PUMPFUN_SOLANA.value
                    and row["status"] != "healthy"
                ]
                upsert_runtime_status(
                    conn,
                    source_key="opportunity:evm_quote",
                    status="degraded" if evm_quote_failures else "healthy",
                    blocker_code=(
                        "EVM_QUOTE_PROVIDER_UNAVAILABLE"
                        if evm_quote_failures
                        else None
                    ),
                    heartbeat_at=now,
                    queue_depth=int(evm_probe_metrics["queue_depth"]),
                    detail=evm_probe_metrics,
                )
                ingestion_paused, source_tasks = await _transition_ingestion_tasks(
                    pause_ingestion=storage.pause_ingestion,
                    ingestion_paused=ingestion_paused,
                    source_tasks=source_tasks,
                    start_source_tasks=start_source_tasks,
                    tracked_tasks=tasks,
                )
                next_maintenance = loop.time() + settings.cleanup_interval_s
            timings.observe(
                "main_loop",
                time.monotonic() - main_loop_started_s,
                limit_s=1.0,
            )
            if loop.time() >= next_runtime_status:
                timing_snapshot = timings.snapshot()
                over_limit = any(
                    bool(item["last_over_limit"])
                    for item in timing_snapshot.values()
                )
                now = real_utc_now_iso()
                upsert_runtime_status(
                    conn,
                    source_key="opportunity:runtime",
                    status="degraded" if over_limit else "healthy",
                    blocker_code="RUNTIME_LANE_SLOW" if over_limit else None,
                    heartbeat_at=now,
                    queue_depth=len(runtime.queue),
                    detail={
                        "worker_started_at": worker_started_at,
                        "timings": timing_snapshot,
                        "dropped_by_source": dict(runtime.dropped_by_source),
                    },
                )
                next_runtime_status = loop.time() + 1.0
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await rpc_governor.close()
        await recovery_rpc_governor.close()
        await robinhood_rpc_governor.close()
        solana_probe.close()
        evm_probe.close()
        insights_conn.close()
        conn.close()
        lock_file.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate default-off config and exit")
    parser.add_argument(
        "--enabled-check",
        action="store_true",
        help="load project env and return success only when capture is enabled",
    )
    args = parser.parse_args(argv)
    load_env_files(Path(os.environ.get("SNIPER_STATE_ROOT", ROOT)).expanduser().resolve())
    settings = load_memecoin_opportunity_settings()
    if args.enabled_check:
        return 0 if settings.enabled else 3
    if args.check:
        print(json.dumps({"enabled": settings.enabled, "db_path": settings.db_path}))
        return 0
    try:
        asyncio.run(run(settings))
    except Exception as exc:
        print(f"opportunity worker stopped: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

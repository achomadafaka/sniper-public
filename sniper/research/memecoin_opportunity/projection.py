"""Immutable Radar evaluation persistence and read-only projection."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Mapping

from .curve_math import CONSTANT_PRODUCT_QUOTE_VERSION, ClipQuote
from .directional import DirectionalVerdict, RadarZone
from .identity import canonical_json, content_fingerprint, require_utc_iso
from .paper_adapter import ADAPTER_VERSION, FILL_MODEL_VERSION, PAPER_RULE_VERSION
from .state import LaunchState
from .store import _insert_immutable, transaction
from .types import VenueId
from .venue_policy import is_active_capture_venue, venue_authority


_DISTANCE = {
    RadarZone.OUTER: 10_000,
    RadarZone.TRACKING: 7_500,
    RadarZone.APPROACHING: 5_000,
    RadarZone.INNER: 2_500,
    RadarZone.CENTER: 0,
}

_PAPER_EVALUATOR_VERSION = "alert-outcome-v4"


def paper_entry_test_summary(conn) -> dict[str, object]:
    """Exact current-contract counts for Radar's realistic-entry measurement."""
    where = (
        "r.network_id='solana' AND r.venue_id='pumpfun_solana' "
        "AND r.rule_version=? AND r.evaluator_version=? AND r.adapter_version=? "
        "AND r.fill_model_version=? AND r.clip_usd_micros=25000000"
    )
    params = (
        PAPER_RULE_VERSION,
        _PAPER_EVALUATOR_VERSION,
        ADAPTER_VERSION,
        FILL_MODEL_VERSION,
    )
    row = conn.execute(
        "SELECT COUNT(*) AS signals_tested,"
        "SUM(f.status='filled') AS paper_buys,"
        "SUM(f.status='entry_unfilled') AS could_not_buy,"
        "SUM(f.receipt_seq IS NULL) AS awaiting_quote,"
        "MAX(f.receipt_at) AS latest_receipt_at "
        "FROM op_paper_fill_requests r LEFT JOIN op_paper_fills f USING(request_id) "
        f"WHERE {where}",
        params,
    ).fetchone()
    reasons = {
        reason["unfilled_reason"]: int(reason["count"])
        for reason in conn.execute(
            "SELECT f.unfilled_reason,COUNT(*) AS count "
            "FROM op_paper_fill_requests r JOIN op_paper_fills f USING(request_id) "
            f"WHERE {where} AND f.status='entry_unfilled' "
            "GROUP BY f.unfilled_reason ORDER BY f.unfilled_reason",
            params,
        )
    }
    return {
        "signals_tested": int(row["signals_tested"] or 0),
        "paper_buys": int(row["paper_buys"] or 0),
        "could_not_buy": int(row["could_not_buy"] or 0),
        "awaiting_quote": int(row["awaiting_quote"] or 0),
        "unfilled_reasons": reasons,
        "latest_receipt_at": row["latest_receipt_at"],
    }


def _arm_fields(rule_version: str) -> dict[str, str]:
    if rule_version == "pump-clean-start-v1":
        return {
            "arm_key": "pump_clean_start",
            "arm_label": "PAPER QUALIFIED — CLEAN START",
            "action_class": "fire_now",
        }
    if rule_version == "pump-curve-break-v1":
        return {
            "arm_key": "pump_curve_break",
            "arm_label": "PAPER QUALIFIED — CURVE BREAK",
            "action_class": "fire_now",
        }
    if rule_version == "pump-ignition-v1":
        return {
            "arm_key": "pump_ignition",
            "arm_label": "PAPER QUALIFIED — IGNITION",
            "action_class": "fire_now",
        }
    if rule_version == "pump-confirmation-v1":
        return {
            "arm_key": "pump_confirmation",
            "arm_label": "PAPER QUALIFIED — CONFIRMED",
            "action_class": "fire_now",
        }
    if rule_version == "pump-confirmation-truth-v3":
        return {
            "arm_key": "pump_confirmation_v3",
            "arm_label": "PAPER QUALIFIED — CONFIRMED",
            "action_class": "fire_now",
        }
    if rule_version == "pump-confirmation-truth-v4":
        return {
            "arm_key": "pump_confirmation_v4",
            "arm_label": "DIAGNOSTIC CONFIRMATION",
            "action_class": "research_only",
        }
    if rule_version == "pump-confirmation-delayed-v2":
        return {
            "arm_key": "pump_confirmation_delayed",
            "arm_label": "DELAYED CONTROL",
            "action_class": "research_only",
        }
    if rule_version == "route-control-v1":
        return {
            "arm_key": "route_control",
            "arm_label": "ROUTE CONTROL",
            "action_class": "research_only",
        }
    if rule_version == "pump-confirmation-truth-v5":
        return {
            "arm_key": "pump_confirmation_v5",
            "arm_label": "DIAGNOSTIC CONFIRMATION",
            "action_class": "research_only",
        }
    if rule_version == "pump-confirmation-truth-v6":
        return {
            "arm_key": "pump_confirmation_v6",
            "arm_label": "DIAGNOSTIC CONFIRMATION",
            "action_class": "research_only",
        }
    if rule_version == "pump-creator-prior-v1":
        return {
            "arm_key": "pump_creator_prior",
            "arm_label": "CREATOR PRIOR",
            "action_class": "research_only",
        }
    if rule_version == "route-control-v2":
        return {
            "arm_key": "route_control_v2",
            "arm_label": "MATCHED CONTROL",
            "action_class": "research_only",
        }
    if rule_version in {"bags-confirmation-v1", "pons-confirmation-v1"}:
        launchpad = rule_version.split("-", 1)[0].upper()
        return {
            "arm_key": rule_version.replace("-", "_"),
            "arm_label": f"{launchpad} DIAGNOSTIC CONFIRMATION",
            "action_class": "research_only",
        }
    if rule_version in {"bags-route-control-v1", "pons-route-control-v1"}:
        launchpad = rule_version.split("-", 1)[0].upper()
        return {
            "arm_key": rule_version.replace("-", "_"),
            "arm_label": f"{launchpad} ROUTE CONTROL",
            "action_class": "research_only",
        }
    return {"arm_key": "none", "arm_label": "WAIT", "action_class": "wait"}


def forward_alert_id(
    launch_id: str,
    rule_version: str,
    clip_usd_micros: int,
) -> str:
    return content_fingerprint(
        {
            "launch_id": str(launch_id),
            "rule_version": str(rule_version),
            "clip_usd_micros": int(clip_usd_micros),
        }
    )


def append_forward_alert_receipt(
    conn,
    *,
    state: LaunchState,
    snapshot_id: str,
    observed_at: str,
    clip_usd_micros: int,
    rule_version: str,
    quote_provider: str,
    round_trip_return_bps: int | None,
    truth_label: str,
) -> str:
    """Append one explicit immutable paper-arm receipt and return its identity."""
    observed = require_utc_iso(observed_at)
    snapshot = conn.execute(
        "SELECT launch_id FROM op_state_snapshots WHERE snapshot_id=?",
        (snapshot_id,),
    ).fetchone()
    if snapshot is None or snapshot["launch_id"] != state.launch_id:
        raise ValueError("alert snapshot launch identity is unavailable")
    launch = conn.execute(
        "SELECT network_id,venue_id,token_id FROM op_launches WHERE launch_id=?",
        (state.launch_id,),
    ).fetchone()
    if launch is None:
        raise ValueError("alert launch identity is unavailable")
    alert_id = forward_alert_id(state.launch_id, rule_version, clip_usd_micros)
    values = {
        "alert_id": alert_id,
        "launch_id": state.launch_id,
        "network_id": launch["network_id"],
        "venue_id": launch["venue_id"],
        "token_id": launch["token_id"],
        "snapshot_id": snapshot_id,
        "observed_at": observed,
        "clip_usd_micros": int(clip_usd_micros),
        "rule_version": str(rule_version),
        "quote_provider": str(quote_provider),
        "round_trip_return_bps": round_trip_return_bps,
        "truth_label": str(truth_label),
    }
    with transaction(conn):
        _insert_immutable(
            conn,
            table="op_forward_alerts",
            identity_column="alert_id",
            identity_value=alert_id,
            values=values,
        )
    return alert_id


def append_forward_alert_entry(
    conn,
    *,
    alert_id: str,
    entry_id: str,
    snapshot_id: str,
    quote_observed_at: str,
    quote_completed_at: str,
    quote_age_ms: int,
    entry_effective_at: str,
    provider: str,
    evaluator_version: str,
) -> bool:
    age = int(quote_age_ms)
    if age < 0:
        raise ValueError("quote_age_ms must be >= 0")
    observed = require_utc_iso(quote_observed_at)
    completed = require_utc_iso(quote_completed_at)
    effective = require_utc_iso(entry_effective_at)
    alert = conn.execute(
        "SELECT snapshot_id FROM op_forward_alerts WHERE alert_id=?",
        (alert_id,),
    ).fetchone()
    if alert is None or alert[0] != snapshot_id:
        raise ValueError("alert snapshot identity is unavailable")
    entry = conn.execute(
        "SELECT entry_effective_at FROM op_counterfactual_entries WHERE entry_id=?",
        (entry_id,),
    ).fetchone()
    if entry is None or entry[0] != effective:
        raise ValueError("counterfactual entry identity is unavailable")
    values = {
        "alert_id": str(alert_id),
        "entry_id": str(entry_id),
        "snapshot_id": str(snapshot_id),
        "quote_observed_at": observed,
        "quote_completed_at": completed,
        "quote_age_ms": age,
        "entry_effective_at": effective,
        "provider": str(provider),
        "evaluator_version": str(evaluator_version),
    }
    with transaction(conn):
        return _insert_immutable(
            conn,
            table="op_forward_alert_entries",
            identity_column="alert_id",
            identity_value=str(alert_id),
            values=values,
        )


def _state_payload(
    state: LaunchState,
    features: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload = asdict(state)
    payload["venue_id"] = state.venue_id.value
    payload["lifecycle"] = state.lifecycle.value
    payload["effective_buyer_count"] = len(state.unique_buyers)
    payload["effective_buyer_hhi_bps"] = state.effective_buyer_hhi_bps
    payload["eligible"] = state.eligible
    if features:
        collisions = {
            key for key in set(payload).intersection(features)
            if payload[key] != features[key]
        }
        if collisions:
            raise ValueError(f"derived features conflict with state: {sorted(collisions)}")
        payload.update(features)
    return payload


def persist_venue_event_facts(conn, canonical_event_id: str, decoded) -> bool:
    facts = asdict(decoded)
    if decoded.event_kind is not None:
        facts["event_kind"] = decoded.event_kind.value
    facts["lifecycle"] = decoded.lifecycle.value
    if decoded.blocker is not None:
        facts["blocker"] = decoded.blocker.value
    values = {
        "canonical_event_id": canonical_event_id,
        "decoder_version": decoded.decoder_version,
        "facts_json": canonical_json(facts),
    }
    with transaction(conn):
        return _insert_immutable(
            conn,
            table="op_venue_event_facts",
            identity_column="canonical_event_id",
            identity_value=canonical_event_id,
            values=values,
        )


def persist_state_evaluation(
    conn,
    *,
    state: LaunchState,
    observed_event_id: str,
    clip_quotes: Mapping[int, ClipQuote],
    verdict: DirectionalVerdict,
    observed_at: str,
    dominant_risk: str,
    features: Mapping[str, object] | None = None,
    remote_probe=None,
    market_input_as_of: str | None = None,
    network_cost_quote_atomic: int = 0,
    snapshot_state_version: str | None = None,
    alert_round_trip_return_bps: int | None = None,
) -> str:
    observed = require_utc_iso(observed_at)
    state_payload = _state_payload(state, features)
    persisted_state_version = snapshot_state_version or state.state_version
    snapshot_id = content_fingerprint(
        {
            "launch_id": state.launch_id,
            "observed_event_id": observed_event_id,
            "observed_at": observed,
            "state": state_payload,
            "state_version": persisted_state_version,
        }
    )
    snapshot_values = {
        "snapshot_id": snapshot_id,
        "launch_id": state.launch_id,
        "observed_event_id": observed_event_id,
        "observed_at": observed,
        "state_json": canonical_json(state_payload),
        "blocker_codes_json": canonical_json(state.blocker_codes),
        "state_version": persisted_state_version,
    }
    prediction_id = content_fingerprint(
        {"snapshot_id": snapshot_id, "rule_version": verdict.rule_version}
    )
    prediction_values = {
        "prediction_id": prediction_id,
        "snapshot_id": snapshot_id,
        "zone": verdict.zone.value,
        "would_fire": int(verdict.would_fire),
        "reason_codes_json": canonical_json(verdict.reason_codes),
        "rule_version": verdict.rule_version,
        "truth_label": verdict.truth_label,
        "distance_bps": _DISTANCE[verdict.zone],
        "dominant_risk": str(dominant_risk),
        "observed_at": observed,
    }
    with transaction(conn):
        _insert_immutable(
            conn,
            table="op_state_snapshots",
            identity_column="snapshot_id",
            identity_value=snapshot_id,
            values=snapshot_values,
        )
        for clip_usd_micros, quote in sorted(clip_quotes.items()):
            quote_id = content_fingerprint(
                {
                    "snapshot_id": snapshot_id,
                    "clip_usd_micros": clip_usd_micros,
                    "quote_version": CONSTANT_PRODUCT_QUOTE_VERSION,
                    "is_remote": False,
                }
            )
            quote_values = {
                "quote_id": quote_id,
                "snapshot_id": snapshot_id,
                "clip_usd_micros": int(clip_usd_micros),
                "quote_in_atomic": str(quote.quote_in_atomic),
                "tokens_out_atomic": str(quote.tokens_out_atomic),
                "immediate_exit_quote_atomic": str(quote.immediate_exit_quote_atomic),
                "round_trip_return_bps": quote.round_trip_return_bps,
                "entry_price_impact_bps": quote.entry_price_impact_bps,
                "status": "executable" if quote.executable else "blocked",
                "blocker_code": quote.blocker,
                "provider": "local_curve_math",
                "is_remote": 0,
                "latency_ms": 0,
                "quote_version": CONSTANT_PRODUCT_QUOTE_VERSION,
                "market_input_as_of": market_input_as_of,
                "network_cost_quote_atomic": str(network_cost_quote_atomic),
                "observed_at": observed,
            }
            _insert_immutable(
                conn,
                table="op_quote_observations",
                identity_column="quote_id",
                identity_value=quote_id,
                values=quote_values,
            )
        primary = clip_quotes.get(25_000_000)
        if remote_probe is not None and (primary is not None or remote_probe.quote_in_atomic):
            remote_id = content_fingerprint(
                {
                    "snapshot_id": snapshot_id,
                    "clip_usd_micros": 25_000_000,
                    "provider": remote_probe.provider,
                    "is_remote": True,
                }
            )
            remote_ok = bool(
                remote_probe.entry_quote_ok and remote_probe.acquired_quantity_sell_ok
            )
            _insert_immutable(
                conn,
                table="op_quote_observations",
                identity_column="quote_id",
                identity_value=remote_id,
                values={
                    "quote_id": remote_id,
                    "snapshot_id": snapshot_id,
                    "clip_usd_micros": 25_000_000,
                    "quote_in_atomic": str(
                        primary.quote_in_atomic
                        if primary is not None else remote_probe.quote_in_atomic
                    ),
                    "tokens_out_atomic": (
                        str(remote_probe.tokens_out_atomic)
                        if remote_probe.tokens_out_atomic is not None else None
                    ),
                    "immediate_exit_quote_atomic": (
                        str(remote_probe.immediate_exit_quote_atomic)
                        if remote_probe.immediate_exit_quote_atomic is not None else None
                    ),
                    "round_trip_return_bps": remote_probe.round_trip_return_bps,
                    "entry_price_impact_bps": None,
                    "status": "executable" if remote_ok else "blocked",
                    "blocker_code": None if remote_ok else (remote_probe.blocker_code or "REMOTE_ROUTE_FAILED"),
                    "provider": remote_probe.provider,
                    "is_remote": 1,
                    "latency_ms": remote_probe.latency_ms,
                    "quote_version": (
                        "remote-acquired-quantity-probe-v3"
                        if remote_probe.provider == "jupiter_quote_v3"
                        else "remote-acquired-quantity-probe-v4"
                        if remote_probe.provider in {
                            "bags_curve_eth_call_v2",
                            "pons_v3_quoter_v1",
                        }
                        else "remote-acquired-quantity-probe-v1"
                    ),
                    "market_input_as_of": market_input_as_of,
                    "network_cost_quote_atomic": str(network_cost_quote_atomic),
                    "observed_at": observed,
                },
            )
        _insert_immutable(
            conn,
            table="op_predictions",
            identity_column="prediction_id",
            identity_value=prediction_id,
            values=prediction_values,
        )
        remote_ok = bool(
            remote_probe is not None
            and remote_probe.entry_quote_ok
            and remote_probe.acquired_quantity_sell_ok
        )
        if verdict.would_fire and remote_ok:
            append_forward_alert_receipt(
                conn,
                state=state,
                snapshot_id=snapshot_id,
                observed_at=observed,
                clip_usd_micros=25_000_000,
                rule_version=verdict.rule_version,
                quote_provider=remote_probe.provider,
                # The receipt must carry the drag the rule actually decided on.
                # Under v4 that is the modeled local round-trip cost, not the
                # paced Jupiter pair the probe happens to return.
                round_trip_return_bps=(
                    alert_round_trip_return_bps
                    if alert_round_trip_return_bps is not None
                    else remote_probe.round_trip_return_bps
                ),
                truth_label=verdict.truth_label,
            )
    return snapshot_id


def upsert_runtime_status(
    conn,
    *,
    source_key: str,
    status: str,
    heartbeat_at: str,
    blocker_code: str | None = None,
    last_event_at: str | None = None,
    queue_depth: int = 0,
    dropped_events: int = 0,
    detail: Mapping[str, object] | None = None,
) -> int:
    heartbeat = require_utc_iso(heartbeat_at)
    if last_event_at is not None:
        last_event_at = require_utc_iso(last_event_at)
    with transaction(conn):
        row = conn.execute(
            "SELECT version FROM op_runtime_status WHERE source_key=?", (source_key,)
        ).fetchone()
        version = (row[0] if row else 0) + 1
        conn.execute(
            "INSERT INTO op_runtime_status "
            "(source_key,status,blocker_code,last_event_at,heartbeat_at,queue_depth,dropped_events,detail_json,version) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET "
            "status=excluded.status,blocker_code=excluded.blocker_code,"
            "last_event_at=COALESCE(excluded.last_event_at,op_runtime_status.last_event_at),"
            "heartbeat_at=excluded.heartbeat_at,queue_depth=excluded.queue_depth,"
            "dropped_events=excluded.dropped_events,detail_json=excluded.detail_json,version=excluded.version",
            (
                source_key, status, blocker_code, last_event_at, heartbeat,
                int(queue_depth), int(dropped_events), canonical_json(detail or {}), version,
            ),
        )
    return version


def _seconds_between(later: str, earlier: str) -> float:
    left = datetime.fromisoformat(require_utc_iso(later).replace("Z", "+00:00"))
    right = datetime.fromisoformat(require_utc_iso(earlier).replace("Z", "+00:00"))
    return max(0.0, (left - right).total_seconds())


def _optional_int(value) -> int | None:
    """A declared bound only counts when the writer actually declared a number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Only venue sockets declare `transport_stale_after_s`. Everything else — the
# `opportunity:*` maintenance workers and the `<venue>:pricing` refresh — rides the
# sweep loop, whose real period is ~19s under load, not the 5s socket bound. Grading
# those against 5s made every snapshot permanently `stale`, which is exactly why a
# 5-hour-dead bags_solana transport went unnoticed: the flag carried no signal.
WORKER_HEARTBEAT_STALE_AFTER_S = 120.0

_RETIRED_RUNTIME_SOURCES = frozenset({
    "opportunity:bags_coverage",
    "opportunity:bags_feed_recovery",
    # The paper sampler moved to `opportunity:paper_sampler_v2` because a window seals
    # permanently and v1 measured provider load from before `radar-realistic-v3`. Nothing
    # writes the v1 key again, so its heartbeat ages without bound and it graded
    # `degraded` forever -- a flag with no signal, which is the failure the note above
    # describes. The row itself stays in the table as the historical record.
    "opportunity:paper_sampler",
})


def _runtime_source_is_current(source_key: str) -> bool:
    if source_key in _RETIRED_RUNTIME_SOURCES:
        return False
    prefix = source_key.split(":", 1)[0]
    try:
        venue = VenueId(prefix)
    except ValueError:
        return True
    return is_active_capture_venue(venue)


def _capture_mode(source_key: str, *, transport_stale: bool) -> str | None:
    """Report live state for a current venue transport."""
    if not source_key.endswith(":ws"):
        return None
    return "down" if transport_stale else "live"


def read_radar_snapshot(conn, *, now_iso: str, stale_after_s: float = 5.0) -> dict[str, object]:
    now = require_utc_iso(now_iso)
    runtime = []
    unhealthy_venues: set[str] = set()
    health_rows = [
        row
        for row in conn.execute(
            "SELECT * FROM op_runtime_status ORDER BY source_key"
        ).fetchall()
        if _runtime_source_is_current(str(row["source_key"]))
    ]
    for row in health_rows:
        item = dict(row)
        heartbeat_age_s = _seconds_between(now, row["heartbeat_at"])
        try:
            detail = json.loads(row["detail_json"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        # `stale_after_s` bounds opportunity-row age, a different question. A source
        # that does not declare its own bound is a loop, not a socket.
        try:
            transport_stale_after_s = float(detail["transport_stale_after_s"])
            if transport_stale_after_s <= 0:
                transport_stale_after_s = WORKER_HEARTBEAT_STALE_AFTER_S
        except (KeyError, TypeError, ValueError):
            transport_stale_after_s = WORKER_HEARTBEAT_STALE_AFTER_S
        transport_stale = heartbeat_age_s > transport_stale_after_s
        event_reference = (
            detail.get("last_chain_event_at")
            or row["last_event_at"]
            or detail.get("event_expectation_started_at")
        )
        event_age_s = (
            _seconds_between(now, event_reference)
            if event_reference is not None else None
        )
        try:
            expected_event_max_age_s = float(detail["expected_event_max_age_s"])
            if expected_event_max_age_s <= 0:
                expected_event_max_age_s = None
        except (KeyError, TypeError, ValueError):
            expected_event_max_age_s = None
        event_stale = (
            event_age_s > expected_event_max_age_s
            if event_age_s is not None and expected_event_max_age_s is not None
            else None
        )
        # A polled lane can hold a live socket, heartbeat every second, decode events and
        # still be twenty minutes behind head. Neither transport age nor event age sees
        # that: both measure when we last *received* something, never how old it was on
        # the chain. Only a writer that declares both numbers can be graded on scan lag,
        # so a source that declares nothing keeps its previous grade.
        blocks_behind_head = _optional_int(detail.get("blocks_behind_head"))
        max_blocks_behind_head = _optional_int(detail.get("max_blocks_behind_head"))
        scan_behind = (
            blocks_behind_head > max_blocks_behind_head
            if blocks_behind_head is not None
            and max_blocks_behind_head is not None
            and max_blocks_behind_head > 0
            else None
        )
        # A source that declares a SCAN contract is graded on it, and its event age is
        # activity data only: Pons produced no event after 2026-08-12 while its shared
        # scan stayed current with zero open gaps, so an event-age bound graded a working
        # lane degraded and no backfill could ever clear it.
        #
        # A source with NO scan contract keeps event age as its delivery check. Pump is
        # that case, and it is the lane with the documented silent failure: on 2026-08-07
        # publicnode accepted the `logsSubscribe`, returned a subscription id, and
        # delivered zero events while the socket heartbeated and the lane read healthy
        # for twenty minutes. Removing event age everywhere took that detector away.
        #
        # A current scan still cannot prove correct topics or decoding. That needs an
        # independent known-log canary; until it exists, event age is the only delivery
        # evidence a scan-less source has.
        source_stale = (
            transport_stale
            or scan_behind is True
            or (scan_behind is None and event_stale is True)
        )
        item["heartbeat_age_s"] = heartbeat_age_s
        item["transport_stale_after_s"] = transport_stale_after_s
        item["transport_stale"] = transport_stale
        item["last_event_at"] = event_reference
        item["event_age_s"] = event_age_s
        item["expected_event_max_age_s"] = expected_event_max_age_s
        item["event_stale"] = event_stale
        item["blocks_behind_head"] = blocks_behind_head
        item["max_blocks_behind_head"] = max_blocks_behind_head
        item["scan_behind"] = scan_behind
        item["event_freshness_status"] = (
            "unknown" if event_age_s is None
            else "stale" if event_stale is True
            else "awaiting_first_event" if event_reference is None
            else "fresh" if event_stale is False
            else "observed"
        )
        item["stale"] = source_stale
        # The persisted `status` column only tracks whether the writer reported a
        # blocker. It stays `healthy` through a dead transport, so derive the
        # operator-facing grade from the staleness the projection already computes.
        raw_status = str(row["status"])
        item["health_status"] = (
            raw_status if raw_status != "healthy"
            else "blocked" if row["blocker_code"]
            else "degraded" if source_stale
            else "healthy"
        )
        source_prefix = str(row["source_key"]).split(":", 1)[0]
        try:
            item["venue_authority"] = venue_authority(VenueId(source_prefix)).value
        except ValueError:
            pass
        item["capture_mode"] = _capture_mode(
            str(row["source_key"]),
            transport_stale=transport_stale,
        )
        runtime.append(item)
        if source_stale or row["status"] != "healthy" or row["blocker_code"]:
            unhealthy_venues.add(str(row["source_key"]).split(":", 1)[0])
    rows = conn.execute(
        "SELECT p.*,s.launch_id,s.state_json,s.blocker_codes_json,l.network_id,l.venue_id,"
        "l.token_id,l.creator_id,l.launched_at FROM op_predictions p "
        "JOIN op_state_snapshots s ON s.snapshot_id=p.snapshot_id "
        "JOIN op_canonical_events event ON event.canonical_event_id=s.observed_event_id "
        "JOIN op_launches l ON l.launch_id=s.launch_id "
        "WHERE event.confirmation!='orphaned' AND NOT EXISTS ("
        "SELECT 1 FROM op_canonical_events correction "
        "WHERE correction.correction_of=event.canonical_event_id "
        "AND correction.confirmation='orphaned') "
        "ORDER BY p.observed_at DESC,p.inserted_at DESC"
    ).fetchall()
    latest = []
    seen = set()
    for row in rows:
        if row["launch_id"] in seen:
            continue
        seen.add(row["launch_id"])
        facts = json.loads(row["state_json"])
        quote_rows = conn.execute(
            "SELECT clip_usd_micros,quote_in_atomic,tokens_out_atomic,immediate_exit_quote_atomic,"
            "round_trip_return_bps,entry_price_impact_bps,status,blocker_code,provider,is_remote,latency_ms,"
            "market_input_as_of,network_cost_quote_atomic "
            "FROM op_quote_observations WHERE snapshot_id=? ORDER BY clip_usd_micros,is_remote",
            (row["snapshot_id"],),
        ).fetchall()
        age_s = _seconds_between(now, row["observed_at"])
        historical_would_fire = bool(row["would_fire"])
        stale = age_s > stale_after_s or row["venue_id"] in unhealthy_venues
        arm = _arm_fields(str(row["rule_version"]))
        latest.append(
            {
                "launch_id": row["launch_id"],
                "network_id": row["network_id"],
                "venue_id": row["venue_id"],
                "token_id": row["token_id"],
                "creator_id": row["creator_id"],
                "launched_at": row["launched_at"],
                "observed_at": row["observed_at"],
                "age_since_update_s": age_s,
                "stale": stale,
                "zone": row["zone"],
                "distance_bps": row["distance_bps"],
                "inward_velocity_bps_per_s": facts.get("inward_velocity_bps_per_s"),
                "zone_transition": facts.get("zone_transition"),
                "data_quality_bps": facts.get("data_quality_bps"),
                "would_fire": (
                    historical_would_fire
                    and not stale
                    and arm["action_class"] == "fire_now"
                ),
                "historical_would_fire": historical_would_fire,
                "dominant_risk": row["dominant_risk"],
                "reason_codes": json.loads(row["reason_codes_json"]),
                "rule_version": row["rule_version"],
                "truth_label": row["truth_label"],
                "facts": facts,
                "blocker_codes": json.loads(row["blocker_codes_json"]),
                "clip_quotes": [dict(item) for item in quote_rows],
                **arm,
            }
        )
    active = [item for item in latest if not item["stale"]]
    history = [item for item in latest if item["stale"]]
    active.sort(
        key=lambda item: (
            item["distance_bps"],
            -int(item.get("inward_velocity_bps_per_s") or 0),
            -int(item.get("data_quality_bps") or 0),
            item["age_since_update_s"],
            item["launch_id"],
        )
    )
    # Newest-stale first. Sorting oldest-first and then capping at 50 kept the 50 rows
    # least worth drawing and dropped every launch that had just aged out of `active`,
    # which is the only part of `history` a live board can use.
    history.sort(key=lambda item: (item["age_since_update_s"], item["launch_id"]))
    zones = {zone.value: 0 for zone in RadarZone}
    for item in active:
        zones[item["zone"]] += 1
    capture_health = [
        item for item in runtime if str(item["source_key"]).endswith(":ws")
    ]
    global_health = capture_health or [
        item for item in runtime
        if not str(item["source_key"]).startswith("opportunity:")
    ]
    global_failures = [
        item for item in global_health
        if bool(item["stale"]) or item["health_status"] != "healthy"
    ]
    stale = bool(global_health) and len(global_failures) == len(global_health)
    stale_sources = (
        [str(item["source_key"]) for item in global_failures] if stale else []
    )
    alerts = []
    for row in conn.execute(
        "SELECT a.alert_id,a.launch_id,a.network_id,a.venue_id,a.token_id,a.observed_at,"
        "a.clip_usd_micros,a.rule_version,a.quote_provider,a.round_trip_return_bps,"
        "a.truth_label,s.state_json FROM op_forward_alerts a "
        "JOIN op_state_snapshots s ON s.snapshot_id=a.snapshot_id "
        "ORDER BY a.observed_at DESC,a.alert_id DESC LIMIT 20"
    ).fetchall():
        item = dict(row)
        facts = json.loads(item.pop("state_json"))
        item.update(_arm_fields(str(item["rule_version"])))
        item["launch_age_ms"] = facts.get("launch_age_ms")
        item["progress_bps"] = facts.get("progress_bps")
        item["effective_buyer_count"] = facts.get("effective_buyer_count")
        alerts.append(item)
    candidate_alerts = sum(1 for item in alerts if item["action_class"] == "fire_now")
    control_receipts = sum(1 for item in alerts if item["action_class"] == "research_only")
    return {
        "schema_version": 1,
        "mode": "EXPLORATORY — EDGE NOT YET ESTABLISHED",
        "as_of": now,
        "stale": stale,
        # `stale` alone told an operator nothing about what was wrong. Name it.
        "stale_sources": stale_sources,
        "reason": (
            f"stale sources: {', '.join(stale_sources)}" if stale_sources else None
        ),
        "opportunities": active,
        "history": history[:50],
        "forward_alerts": alerts,
        "source_health": runtime,
        "summary": {
            "active": len(active),
            "archived": len(history),
            "total": len(latest),
            "alerts": candidate_alerts,
            "control_receipts": control_receipts,
            "would_fire": sum(1 for item in active if item["would_fire"]),
            "zones": zones,
        },
    }

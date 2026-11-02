"""Run the adaptive v3 position policy on the isolated Radar paper ledger."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from sniper.services.memecoin_v6_engine import (
    FRACTION_PPM_SCALE,
    EvidenceIdentity,
    V6Action,
    V6PositionState,
    fingerprint,
    make_observation,
    state_document,
    state_from_document,
    step,
)
from sniper.services.memecoin_v6_policy_v3 import POLICY_V3

from .directional import ADAPTIVE_V3_RULES
from .identity import canonical_json, content_fingerprint, require_utc_iso
from .paper_adapter import (
    ADAPTER_VERSION,
    FILL_MODEL_VERSION,
    PAPER_RULE_BY_VENUE,
    QuoteDeferred,
    QuoteRejected,
    _sqlite_atomic,
)
from .paper_lifecycle import _return_bps, _scored_out_atomic
from .store import ImmutableConflict, transaction


LIFECYCLE_V3 = "radar-adaptive-v3"
# Both source maps are already filtered to scoreable venues, so a retired venue can
# never contribute an arm here.
ADAPTIVE_ARMS = (*ADAPTIVE_V3_RULES, *PAPER_RULE_BY_VENUE.values())
FOLLOWUP_HORIZONS_S = (30, 120, 300, 900)
EVALUATOR_VERSION = "alert-outcome-v4"
MIN_POSITION_TICK_MS = 2_000

ORIGIN_BY_VENUE = {
    "pumpfun_solana": "radar_pump",
    "bags_robinhood_4663": "radar_bags",
    "pons_v1_4663": "radar_pons",
}
TERMINAL_CODE_BY_REASON = {
    "trail": "trail_exit",
    "creator_sale": "creator_exit",
    "exact_stop": "stop_first",
    "time_cap": "time_exit",
    "time_cap:hard": "time_exit",
}

_CONFIG = {
    "arms": list(ADAPTIVE_ARMS),
    "policy_id": POLICY_V3.policy_id,
    "policy_hash": POLICY_V3.hash(),
    "followup_horizons_s": list(FOLLOWUP_HORIZONS_S),
    "execution_haircut_bps_per_leg": 122,
    "forward_only": True,
}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(require_utc_iso(value).replace("Z", "+00:00"))


def activate_adaptive_lifecycle(conn: sqlite3.Connection, *, now_iso: str) -> str:
    """Create the forward cohort boundary once."""
    activated_at = require_utc_iso(now_iso)
    values = {
        "lifecycle_version": LIFECYCLE_V3,
        "activated_at": activated_at,
        "config_json": canonical_json(_CONFIG),
    }
    row_fingerprint = content_fingerprint(values)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO op_paper_lifecycle_activations ("
        "lifecycle_version,activated_at,config_json,row_fingerprint,inserted_at"
        ") VALUES (?,?,?,?,?)",
        (
            LIFECYCLE_V3,
            activated_at,
            values["config_json"],
            row_fingerprint,
            activated_at,
        ),
    )
    if cursor.rowcount == 0:
        row = conn.execute(
            "SELECT 1 FROM op_paper_lifecycle_activations "
            "WHERE lifecycle_version=?",
            (LIFECYCLE_V3,),
        ).fetchone()
        if row is None:
            raise ImmutableConflict("adaptive lifecycle activation disappeared")
    return LIFECYCLE_V3


def _active_entries(conn: sqlite3.Connection, activated_at: str):
    placeholders = ",".join("?" for _ in ADAPTIVE_ARMS)
    return conn.execute(
        "SELECT r.request_id,r.alert_id,r.launch_id,r.network_id,r.venue_id,r.token_id,"
        "r.creator_id,r.rule_version,r.state_fingerprint,"
        "f.quote_out_decimals,f.expected_out_atomic,f.quote_in_asset,"
        "f.quote_in_atomic,f.quote_in_decimals,f.receipt_at,f.route_binding_json "
        "FROM op_paper_fill_requests r JOIN op_paper_fills f USING(request_id) "
        "WHERE f.status='filled' AND f.receipt_at>=? "
        f"AND r.rule_version IN ({placeholders}) "
        "AND r.evaluator_version=? AND r.adapter_version=? "
        "AND r.fill_model_version=? AND r.clip_usd_micros=25000000 "
        "AND NOT EXISTS (SELECT 1 FROM op_paper_outcomes o "
        "WHERE o.request_id=r.request_id AND o.lifecycle_version=?) "
        "ORDER BY f.receipt_at,r.request_id",
        (
            activated_at,
            *ADAPTIVE_ARMS,
            EVALUATOR_VERSION,
            ADAPTER_VERSION,
            FILL_MODEL_VERSION,
            LIFECYCLE_V3,
        ),
    ).fetchall()


def _initial_state(entry) -> V6PositionState:
    quantity = int(entry["expected_out_atomic"])
    entry_unit_price = Decimal(str(entry["quote_in_atomic"])) / Decimal(quantity)
    return V6PositionState(
        position_id=content_fingerprint({
            "request_id": entry["request_id"],
            "lifecycle_version": LIFECYCLE_V3,
        }),
        remaining_qty_atomic=quantity,
        entry_unit_price=format(entry_unit_price, "f"),
        entry_quote_asset=str(entry["quote_in_asset"]),
        opened_at=require_utc_iso(entry["receipt_at"]),
    )


def _load_state(conn: sqlite3.Connection, entry) -> tuple[int, V6PositionState]:
    row = conn.execute(
        "SELECT sequence,state_json FROM op_paper_position_states "
        "WHERE request_id=? AND lifecycle_version=? ORDER BY sequence DESC LIMIT 1",
        (entry["request_id"], LIFECYCLE_V3),
    ).fetchone()
    if row is None:
        return 0, _initial_state(entry)
    return int(row["sequence"]), state_from_document(json.loads(row["state_json"]))


def _quote_request(entry, *, quantity_atomic: int, now_iso: str, request_id: str):
    return {
        "observation_id": request_id,
        "request_id": entry["request_id"],
        "token_id": entry["token_id"],
        "quantity_atomic": int(quantity_atomic),
        "token_decimals": int(entry["quote_out_decimals"]),
        "quote_asset": entry["quote_in_asset"],
        "quote_decimals": int(entry["quote_in_decimals"]),
        "venue_id": entry["venue_id"],
        "route_binding": json.loads(entry["route_binding_json"]),
        "now_iso": now_iso,
    }


def _quote_expected(economics) -> int:
    if not isinstance(economics, dict):
        raise QuoteDeferred
    expected = economics.get("expected_out_atomic")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        raise QuoteRejected("malformed_quote")
    return expected


def _append_preview_observation(
    conn: sqlite3.Connection,
    *,
    entry,
    sequence: int,
    elapsed_ms: int,
    observed_at: str,
    economics: dict[str, object],
) -> str:
    observation_id = content_fingerprint({
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_V3,
        "sequence": sequence,
        "kind": "preview",
    })
    expected = _quote_expected(economics)
    scored = _scored_out_atomic(expected)
    values = {
        "observation_id": observation_id,
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_V3,
        "scheduled_age_ms": elapsed_ms,
        "status": "executable",
        "failure_reason": None,
        "quote_provider": economics.get("quote_provider"),
        "quote_in_asset": economics.get("quote_in_asset"),
        "quote_in_atomic": economics.get("quote_in_atomic"),
        "quote_in_decimals": economics.get("quote_in_decimals"),
        "quote_out_asset": economics.get("quote_out_asset"),
        "quote_out_atomic": entry["quote_in_atomic"],
        "quote_out_decimals": economics.get("quote_out_decimals"),
        "expected_out_atomic": expected,
        "floor_out_atomic": scored,
        "slippage_bps": economics.get("slippage_bps"),
        "route_binding_json": canonical_json(economics.get("route_binding") or {}),
        "quote_payload_json": canonical_json(economics.get("quote_payload") or {}),
        "quote_started_at": economics.get("quote_started_at"),
        "quote_completed_at": economics.get("quote_completed_at"),
        "observed_at": observed_at,
        "return_bps": _return_bps(scored, int(entry["quote_in_atomic"])),
    }
    row_fingerprint = content_fingerprint(values)
    stored = {
        **values,
        "quote_in_atomic": _sqlite_atomic(values["quote_in_atomic"]),
        "quote_out_atomic": _sqlite_atomic(values["quote_out_atomic"]),
        "expected_out_atomic": _sqlite_atomic(values["expected_out_atomic"]),
        "floor_out_atomic": _sqlite_atomic(values["floor_out_atomic"]),
    }
    conn.execute(
        "INSERT INTO op_paper_exit_observations ("
        f"{','.join(values)},row_fingerprint,inserted_at"
        f") VALUES ({','.join('?' for _ in values)},?,?)",
        (*stored.values(), row_fingerprint, observed_at),
    )
    return observation_id


def _build_observation(
    entry,
    state: V6PositionState,
    *,
    facts: dict[str, object],
    economics: dict[str, object],
    quote_evidence_id: str,
    now_iso: str,
):
    expected = _quote_expected(economics)
    scored = _scored_out_atomic(expected)
    current_unit_price = Decimal(scored) / Decimal(state.remaining_qty_atomic)
    held_s = max(0.0, (_parse(now_iso) - _parse(entry["receipt_at"])).total_seconds())
    payload = {
        "is_break": False,
        "held_s": held_s,
        "arrival_rate": 0.0,
        "buy_share": 0.0,
        "realized_vol": 0.0,
        "prior_low": float(current_unit_price),
        "price": float(current_unit_price),
        "net": -1.0,
        "chain_vol_p90": 0.0,
        "route_degraded": False,
        "source_wallet_exited": False,
        "wallet_tier_adjustment": 0,
        "creator_sale_since_entry": False,
        "quote_status": "healthy",
        "tape_complete": True,
        "depth_pull_flag": False,
        "depth_provenance": "trusted",
        "capture_version": "v6-direct-v1",
        "quote_out_asset": entry["quote_in_asset"],
    }
    payload.update(facts)
    payload["executable_price"] = float(current_unit_price)
    payload["quote_status"] = "healthy"
    market_evidence_id = str(facts.get("canonical_event_id") or content_fingerprint({
        "request_id": entry["request_id"],
        "now_iso": now_iso,
        "kind": "market",
    }))
    evidence = EvidenceIdentity(
        market=market_evidence_id,
        quote=quote_evidence_id,
        source=None,
    )
    observation_id = "obs-" + fingerprint({
        "position_id": state.position_id,
        "market": market_evidence_id,
        "quote": quote_evidence_id,
    })[:24]
    return make_observation(
        observation_id=observation_id,
        observed_at=now_iso,
        network_id=str(entry["network_id"]),
        payload=payload,
        evidence=evidence,
    )


def _append_state(
    conn: sqlite3.Connection,
    entry,
    sequence: int,
    state: V6PositionState,
    observed_at: str,
) -> None:
    values = {
        "state_id": content_fingerprint({
            "request_id": entry["request_id"],
            "lifecycle_version": LIFECYCLE_V3,
            "sequence": sequence,
        }),
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_V3,
        "sequence": sequence,
        "state_json": canonical_json(state_document(state)),
        "policy_hash": POLICY_V3.hash(),
        "observed_at": observed_at,
    }
    conn.execute(
        "INSERT INTO op_paper_position_states ("
        f"{','.join(values)},row_fingerprint,inserted_at"
        f") VALUES ({','.join('?' for _ in values)},?,?)",
        (*values.values(), content_fingerprint(values), observed_at),
    )


def _append_action(
    conn: sqlite3.Connection,
    entry,
    sequence: int,
    decision,
    *,
    quantity_atomic: int,
    expected_out_atomic: int,
    scored_out_atomic: int,
    quote_evidence_id: str,
    market_evidence_id: str | None,
    observed_at: str,
) -> None:
    values = {
        "action_id": content_fingerprint({
            "request_id": entry["request_id"],
            "lifecycle_version": LIFECYCLE_V3,
            "sequence": sequence,
            "action_token": decision.action_token,
        }),
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_V3,
        "sequence": sequence,
        "action": decision.action.value,
        "fraction_ppm": decision.fraction_ppm,
        "quantity_atomic": str(quantity_atomic),
        "expected_out_atomic": str(expected_out_atomic),
        "scored_out_atomic": str(scored_out_atomic),
        "reason_codes_json": canonical_json(list(decision.reason_codes)),
        "policy_hash": decision.policy_hash,
        "quote_evidence_id": quote_evidence_id,
        "market_evidence_id": market_evidence_id,
        "observed_at": observed_at,
    }
    conn.execute(
        "INSERT INTO op_paper_position_actions ("
        f"{','.join(values)},row_fingerprint,inserted_at"
        f") VALUES ({','.join('?' for _ in values)},?,?)",
        (*values.values(), content_fingerprint(values), observed_at),
    )


def _result_code(reason_codes: tuple[str, ...]) -> str:
    for reason in reason_codes:
        code = TERMINAL_CODE_BY_REASON.get(reason)
        if code is not None:
            return code
    return "adaptive_exit"


def _sum_scored_proceeds(conn: sqlite3.Connection, entry) -> int:
    rows = conn.execute(
        "SELECT scored_out_atomic "
        "FROM op_paper_position_actions "
        "WHERE request_id=? AND lifecycle_version=?",
        (entry["request_id"], LIFECYCLE_V3),
    ).fetchall()
    return sum(int(row[0]) for row in rows)


def _append_outcome(
    conn: sqlite3.Connection,
    *,
    entry,
    decision,
    observation,
    preview_observation_id: str,
    total_scored: int,
    outcome_at: str,
) -> None:
    values = {
        "outcome_id": content_fingerprint({
            "request_id": entry["request_id"],
            "lifecycle_version": LIFECYCLE_V3,
        }),
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_V3,
        "status": "valid",
        "result_code": _result_code(decision.reason_codes),
        "terminal_observation_id": preview_observation_id,
        "entry_quote_atomic": str(entry["quote_in_atomic"]),
        "exit_floor_atomic": str(total_scored),
        "return_bps": _return_bps(total_scored, int(entry["quote_in_atomic"])),
        "outcome_at": outcome_at,
        "origin_kind": ORIGIN_BY_VENUE[str(entry["venue_id"])],
        "policy_hash": decision.policy_hash,
        "decision_facts_json": canonical_json(dict(observation.payload)),
        "exit_reason_codes_json": canonical_json(list(decision.reason_codes)),
    }
    conn.execute(
        "INSERT INTO op_paper_outcomes ("
        f"{','.join(values)},row_fingerprint,inserted_at"
        f") VALUES ({','.join('?' for _ in values)},?,?)",
        (*values.values(), content_fingerprint(values), outcome_at),
    )


def _schedule_followups(conn: sqlite3.Connection, entry, *, terminal_at: str) -> None:
    terminal = _parse(terminal_at)
    for horizon_s in FOLLOWUP_HORIZONS_S:
        due_at = (terminal + timedelta(seconds=horizon_s)).isoformat()
        values = {
            "followup_id": content_fingerprint({
                "request_id": entry["request_id"],
                "lifecycle_version": LIFECYCLE_V3,
                "horizon_s": horizon_s,
            }),
            "request_id": entry["request_id"],
            "lifecycle_version": LIFECYCLE_V3,
            "horizon_s": horizon_s,
            "due_at": due_at,
            "status": "scheduled",
            "expected_out_atomic": None,
            "return_bps": None,
            "failure_reason": None,
            "observed_at": None,
        }
        conn.execute(
            "INSERT INTO op_paper_exit_followups ("
            f"{','.join(values)},row_fingerprint,inserted_at"
            f") VALUES ({','.join('?' for _ in values)},?,?)",
            (*values.values(), content_fingerprint(values), terminal_at),
        )


def _advance_one(
    conn: sqlite3.Connection,
    entry,
    *,
    quote_fn,
    observation_fn,
    now_iso: str,
) -> str:
    last_tick = conn.execute(
        "SELECT observed_at FROM op_paper_position_states "
        "WHERE request_id=? AND lifecycle_version=? "
        "ORDER BY sequence DESC LIMIT 1",
        (entry["request_id"], LIFECYCLE_V3),
    ).fetchone()
    if last_tick is not None:
        tick_age_ms = int(
            (_parse(now_iso) - _parse(last_tick["observed_at"])).total_seconds()
            * 1_000
        )
        if tick_age_ms < MIN_POSITION_TICK_MS:
            return "skipped"
    sequence, state = _load_state(conn, entry)
    if state.remaining_qty_atomic <= 0:
        return "held"
    preview_id = content_fingerprint({
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_V3,
        "sequence": sequence + 1,
        "kind": "preview-request",
    })
    preview_request = _quote_request(
        entry,
        quantity_atomic=state.remaining_qty_atomic,
        now_iso=now_iso,
        request_id=preview_id,
    )
    try:
        preview = quote_fn(preview_request)
        _quote_expected(preview)
    except (QuoteDeferred, QuoteRejected):
        return "unfilled"
    elapsed_ms = max(
        0,
        int((_parse(now_iso) - _parse(entry["receipt_at"])).total_seconds() * 1_000),
    )
    preview_observation_id = _append_preview_observation(
        conn,
        entry=entry,
        sequence=sequence + 1,
        elapsed_ms=elapsed_ms,
        observed_at=now_iso,
        economics=preview,
    )
    facts = dict(observation_fn(entry, now_iso=now_iso) or {})
    observation = _build_observation(
        entry,
        state,
        facts=facts,
        economics=preview,
        quote_evidence_id=preview_observation_id,
        now_iso=now_iso,
    )
    decision = step(state, observation, policy=POLICY_V3)
    next_sequence = sequence + 1
    if decision.action is V6Action.HOLD:
        _append_state(
            conn, entry, next_sequence, decision.state_if_filled, now_iso,
        )
        return "held"

    requested = min(
        state.remaining_qty_atomic,
        max(
            1,
            state.remaining_qty_atomic * decision.fraction_ppm
            // FRACTION_PPM_SCALE,
        ),
    )
    action_request_id = content_fingerprint({
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_V3,
        "sequence": next_sequence,
        "kind": "action",
    })
    action_request = _quote_request(
        entry,
        quantity_atomic=requested,
        now_iso=now_iso,
        request_id=action_request_id,
    )
    try:
        economics = quote_fn(action_request)
        expected = _quote_expected(economics)
    except (QuoteDeferred, QuoteRejected):
        _append_state(
            conn, entry, next_sequence, decision.state_if_unfilled, now_iso,
        )
        return "unfilled"
    scored = _scored_out_atomic(expected)
    with transaction(conn):
        _append_action(
            conn,
            entry,
            next_sequence,
            decision,
            quantity_atomic=requested,
            expected_out_atomic=expected,
            scored_out_atomic=scored,
            quote_evidence_id=action_request_id,
            market_evidence_id=observation.evidence.market,
            observed_at=now_iso,
        )
        filled = decision.state_if_filled
        _append_state(conn, entry, next_sequence, filled, now_iso)
        if filled.remaining_qty_atomic > 0:
            return "derisked"
        total_scored = _sum_scored_proceeds(conn, entry)
        _append_outcome(
            conn,
            entry=entry,
            decision=decision,
            observation=observation,
            preview_observation_id=preview_observation_id,
            total_scored=total_scored,
            outcome_at=now_iso,
        )
        _schedule_followups(conn, entry, terminal_at=now_iso)
    return "completed"


def _resolve_followups(
    conn: sqlite3.Connection, *, quote_fn, now_iso: str,
) -> int:
    rows = conn.execute(
        "SELECT u.*,r.token_id,r.venue_id,f.quote_out_decimals,"
        "f.quote_in_asset,f.quote_in_atomic,f.quote_in_decimals,f.route_binding_json,"
        "a.quantity_atomic "
        "FROM op_paper_exit_followups u "
        "JOIN op_paper_fill_requests r USING(request_id) "
        "JOIN op_paper_fills f USING(request_id) "
        "JOIN op_paper_position_actions a ON a.request_id=u.request_id "
        "AND a.lifecycle_version=u.lifecycle_version "
        "WHERE u.lifecycle_version=? AND u.status='scheduled' AND u.due_at<=? "
        "AND a.sequence=(SELECT MAX(a2.sequence) FROM op_paper_position_actions a2 "
        "WHERE a2.request_id=u.request_id AND a2.lifecycle_version=u.lifecycle_version) "
        "ORDER BY u.due_at,u.followup_id",
        (LIFECYCLE_V3, now_iso),
    ).fetchall()
    resolved = 0
    for row in rows:
        request = _quote_request(
            row,
            quantity_atomic=int(row["quantity_atomic"]),
            now_iso=now_iso,
            request_id=row["followup_id"],
        )
        try:
            economics = quote_fn(request)
            expected = _quote_expected(economics)
        except QuoteDeferred:
            continue
        except QuoteRejected as exc:
            status = "unavailable"
            expected = None
            return_bps = None
            failure_reason = str(exc) or "quote_unavailable"
        else:
            status = "executable"
            scored = _scored_out_atomic(expected)
            initial_qty = int(conn.execute(
                "SELECT expected_out_atomic FROM op_paper_fills WHERE request_id=?",
                (row["request_id"],),
            ).fetchone()[0])
            basis = max(
                1,
                int(row["quote_in_atomic"]) * int(row["quantity_atomic"])
                // initial_qty,
            )
            return_bps = _return_bps(scored, basis)
            failure_reason = None
        values = {
            "status": status,
            "expected_out_atomic": (
                None if expected is None else str(expected)
            ),
            "return_bps": return_bps,
            "failure_reason": failure_reason,
            "observed_at": now_iso,
        }
        fingerprint_values = {
            "followup_id": row["followup_id"],
            "request_id": row["request_id"],
            "lifecycle_version": row["lifecycle_version"],
            "horizon_s": row["horizon_s"],
            "due_at": row["due_at"],
            **values,
        }
        conn.execute(
            "UPDATE op_paper_exit_followups SET status=?,expected_out_atomic=?,"
            "return_bps=?,failure_reason=?,observed_at=?,row_fingerprint=? "
            "WHERE followup_id=? AND status='scheduled'",
            (
                *values.values(),
                content_fingerprint(fingerprint_values),
                row["followup_id"],
            ),
        )
        resolved += 1
    return resolved


def drive_adaptive_lifecycle(
    conn: sqlite3.Connection,
    *,
    quote_fn,
    observation_fn,
    now_iso: str,
) -> dict[str, int]:
    """Process one exact quote tick for each open forward entry."""
    now = require_utc_iso(now_iso)
    counters = {
        "eligible": 0,
        "held": 0,
        "derisked": 0,
        "completed": 0,
        "unfilled": 0,
        "skipped": 0,
        "followups": 0,
    }
    activation = conn.execute(
        "SELECT activated_at FROM op_paper_lifecycle_activations "
        "WHERE lifecycle_version=?",
        (LIFECYCLE_V3,),
    ).fetchone()
    if activation is None:
        return counters
    counters["followups"] = _resolve_followups(
        conn, quote_fn=quote_fn, now_iso=now,
    )
    for entry in _active_entries(conn, activation["activated_at"]):
        counters["eligible"] += 1
        result = _advance_one(
            conn,
            entry,
            quote_fn=quote_fn,
            observation_fn=observation_fn,
            now_iso=now,
        )
        counters[result] += 1
    return counters


def _empty_result_block() -> dict[str, object]:
    return {
        "valid_results": 0,
        "average_net_return_bps": None,
        "adaptive_exits": 0,
        "trail_exits": 0,
        "creator_exits": 0,
        "stop_exits": 0,
        "time_exits": 0,
        "open": 0,
    }


def adaptive_result_summary(conn: sqlite3.Connection) -> dict[str, object]:
    """Return separate result blocks for each venue and arm."""
    activation = conn.execute(
        "SELECT activated_at FROM op_paper_lifecycle_activations "
        "WHERE lifecycle_version=?",
        (LIFECYCLE_V3,),
    ).fetchone()
    by_venue = {
        venue: _empty_result_block() for venue in PAPER_RULE_BY_VENUE
    }
    by_arm = {arm: _empty_result_block() for arm in ADAPTIVE_ARMS}
    if activation is None:
        return {
            "lifecycle_version": LIFECYCLE_V3,
            "activated_at": None,
            "valid_results": 0,
            "by_venue": by_venue,
            "by_arm": by_arm,
        }

    def load(group_column: str, value: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT SUM(o.status='valid') valid_results,"
            "ROUND(AVG(CASE WHEN o.status='valid' THEN o.return_bps END)) average_return,"
            "SUM(o.result_code='adaptive_exit') adaptive_exits,"
            "SUM(o.result_code='trail_exit') trail_exits,"
            "SUM(o.result_code='creator_exit') creator_exits,"
            "SUM(o.result_code='stop_first') stop_exits,"
            "SUM(o.result_code='time_exit') time_exits "
            "FROM op_paper_outcomes o JOIN op_paper_fill_requests r USING(request_id) "
            f"WHERE o.lifecycle_version=? AND r.{group_column}=?",
            (LIFECYCLE_V3, value),
        ).fetchone()
        arm_placeholders = ",".join("?" for _ in ADAPTIVE_ARMS)
        open_count = conn.execute(
            "SELECT COUNT(*) FROM op_paper_fill_requests r "
            "JOIN op_paper_fills f USING(request_id) "
            "WHERE f.status='filled' AND f.receipt_at>=? "
            f"AND r.{group_column}=? "
            f"AND r.rule_version IN ({arm_placeholders}) "
            "AND NOT EXISTS (SELECT 1 FROM op_paper_outcomes o "
            "WHERE o.request_id=r.request_id AND o.lifecycle_version=?)",
            (
                activation["activated_at"],
                value,
                *ADAPTIVE_ARMS,
                LIFECYCLE_V3,
            ),
        ).fetchone()[0]
        return {
            "valid_results": int(row["valid_results"] or 0),
            "average_net_return_bps": (
                None if row["average_return"] is None else int(row["average_return"])
            ),
            "adaptive_exits": int(row["adaptive_exits"] or 0),
            "trail_exits": int(row["trail_exits"] or 0),
            "creator_exits": int(row["creator_exits"] or 0),
            "stop_exits": int(row["stop_exits"] or 0),
            "time_exits": int(row["time_exits"] or 0),
            "open": int(open_count or 0),
        }

    for venue in by_venue:
        by_venue[venue] = load("venue_id", venue)
    for arm in by_arm:
        by_arm[arm] = load("rule_version", arm)
    return {
        "lifecycle_version": LIFECYCLE_V3,
        "activated_at": activation["activated_at"],
        "valid_results": sum(
            int(block["valid_results"]) for block in by_venue.values()
        ),
        "by_venue": by_venue,
        "by_arm": by_arm,
    }

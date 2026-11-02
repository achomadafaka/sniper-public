"""Radar-owned realistic paper exits for the V6 confirmation rule and its control.

`radar-realistic-v1` did not measure a fill. It sold the entry `floor_out_atomic` and
scored the exit `floor_out_atomic`, and both of those are `expected_out_atomic * 0.97`
exactly -- verified on all 81 entry fills and all 103 exit observations, every one at
`slippage_bps = 300`. So its return was the expected route return times 0.9409 minus one,
a config constant and not a property of the world. Lowering `slippageBps` to 50 would have
"restored the edge" with nothing about the token changed. The -800 bps stop read a number
that started at about -950 bps, so 26 of 51 positions stopped on their first quote.

`radar-realistic-v2` sells the expected entry quantity, scores the expected exit amount,
and subtracts a MEASURED execution haircut on each leg. The haircut is p90 of realized
fill-against-quote deviation over 31 funded Solana chain entries at $37.50 to $50
(median +0.058%, mean -0.017%, p10 -1.12%, p90 +1.22%).

The provider threshold is still recorded on every row. It is a safety bound the signer
would set, not a prediction of the fill, so it does not belong in the score.

Known coverage limit: the exit leg's realized deviation is NOT measured. Every chain exit
row in `memecoin_fills` carries a null `execution_slippage_pct`. The haircut applies the
entry-leg distribution to both legs because that is the only measurement that exists.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Mapping

from .identity import canonical_json, content_fingerprint, require_utc_iso
from .paper_adapter import (
    ADAPTER_VERSION,
    FILL_MODEL_VERSION,
    PAPER_RULE_VERSIONS,
    PAPER_RULE_BY_VENUE,
    QuoteDeferred,
    QuoteRejected,
    _sqlite_atomic,
)
from .paper_completion import PaperQuoteCompletion
from .store import ImmutableConflict


LIFECYCLE_VERSION = "radar-realistic-v2"
EVALUATOR_VERSION = "alert-outcome-v4"
TARGET_BPS = 1_200
STOP_BPS = -800
QUOTE_CADENCE_MS = 5_000
EXIT_AGE_MS = 60_000
TERMINAL_GRACE_MS = 30_000
TERMINAL_AGE_MS = EXIT_AGE_MS + TERMINAL_GRACE_MS
# p90 of realized fill-against-quote deviation, 31 funded Solana chain entries.
EXECUTION_HAIRCUT_BPS = 122

_CONFIG = {
    "clip_usd_micros": 25_000_000,
    "target_bps": TARGET_BPS,
    "stop_bps": STOP_BPS,
    "quote_cadence_ms": QUOTE_CADENCE_MS,
    "exit_age_ms": EXIT_AGE_MS,
    "terminal_grace_ms": TERMINAL_GRACE_MS,
    "entry_fill_model_version": FILL_MODEL_VERSION,
    "exit_fill_basis": "provider_expected_minus_measured_execution_haircut",
    "execution_haircut_bps_per_leg": EXECUTION_HAIRCUT_BPS,
    "execution_haircut_source": "funded_solana_chain_entry_slippage_p90_n31",
}


def _scored_out_atomic(expected_out_atomic: int) -> int:
    """Apply the measured execution haircut to both legs of the round trip."""
    scale = 10_000 - EXECUTION_HAIRCUT_BPS
    return int(expected_out_atomic) * scale * scale // (10_000 * 10_000)


def _return_bps(scored_out_atomic: int, quote_in_atomic: int) -> int:
    return (int(scored_out_atomic) - int(quote_in_atomic)) * 10_000 // int(quote_in_atomic)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(require_utc_iso(value).replace("Z", "+00:00"))


def _shift_iso(value: str, delta_ms: int) -> str:
    moved = _parse(value) + timedelta(milliseconds=delta_ms)
    return moved.isoformat().replace("+00:00", "Z")


def activate_paper_lifecycle(conn: sqlite3.Connection, *, now_iso: str) -> str:
    """Create the immutable forward-cohort boundary once."""
    activated_at = require_utc_iso(now_iso)
    values = {
        "lifecycle_version": LIFECYCLE_VERSION,
        "activated_at": activated_at,
        "config_json": canonical_json(_CONFIG),
    }
    fingerprint = content_fingerprint(values)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO op_paper_lifecycle_activations "
        "(lifecycle_version,activated_at,config_json,row_fingerprint,inserted_at) "
        "VALUES (?,?,?,?,?)",
        (LIFECYCLE_VERSION, activated_at, values["config_json"], fingerprint, activated_at),
    )
    if cursor.rowcount == 0:
        row = conn.execute(
            "SELECT row_fingerprint FROM op_paper_lifecycle_activations "
            "WHERE lifecycle_version=?",
            (LIFECYCLE_VERSION,),
        ).fetchone()
        if row is None:
            raise ImmutableConflict("paper lifecycle activation disappeared")
    return LIFECYCLE_VERSION


def _active_entries(
    conn: sqlite3.Connection,
    activated_at: str,
    lifecycle_version: str = LIFECYCLE_VERSION,
    *,
    collect_newer_than_iso: str | None = None,
):
    """Positions this lifecycle still has work for.

    A v2 outcome closes the DECISION, not the observation series. `radar-realistic-v3`
    reads this same series to score the identical positions against an on-time
    first-quote baseline, and 37% of them trip the v2 stop on their first quote. If
    collection stopped with the v2 decision, v3 would have one observation and nothing
    to measure. So a decided position stays in the loop until `collect_newer_than_iso`;
    the caller must not write it a second outcome.

    This RAISES provider load. 56% of v6 positions used to stop early and now keep
    quoting to the 90 s terminal age, up to 17 further buckets each. Any paper-sampler
    window recorded before this change measures the old load.
    """
    rules = ",".join("?" for _ in PAPER_RULE_VERSIONS)
    undecided = (
        "NOT EXISTS (SELECT 1 FROM op_paper_outcomes o "
        "WHERE o.request_id=r.request_id AND o.lifecycle_version=?)"
    )
    tail: tuple[object, ...] = (lifecycle_version,)
    if collect_newer_than_iso is not None:
        undecided = f"(f.receipt_at>? OR {undecided})"
        tail = (collect_newer_than_iso, lifecycle_version)
    return conn.execute(
        "SELECT r.request_id,r.token_id,r.rule_version,r.venue_id,f.quote_out_decimals,"
        "f.expected_out_atomic,f.floor_out_atomic,"
        "f.quote_in_asset,f.quote_in_atomic,f.quote_in_decimals,f.receipt_at,"
        "f.route_binding_json "
        "FROM op_paper_fill_requests r JOIN op_paper_fills f USING(request_id) "
        "WHERE f.status='filled' AND f.receipt_at>=? "
        f"AND r.rule_version IN ({rules}) AND r.evaluator_version=? "
        "AND r.adapter_version=? AND r.fill_model_version=? "
        "AND r.clip_usd_micros=25000000 "
        f"AND {undecided} "
        "ORDER BY f.receipt_at,r.request_id",
        (
            activated_at,
            *PAPER_RULE_VERSIONS,
            EVALUATOR_VERSION,
            ADAPTER_VERSION,
            FILL_MODEL_VERSION,
            *tail,
        ),
    ).fetchall()


def _append_observation(
    conn: sqlite3.Connection,
    *,
    entry,
    scheduled_age_ms: int,
    observed_at: str,
    status: str,
    failure_reason: str | None = None,
    economics: dict[str, object] | None = None,
) -> str:
    observation_id = content_fingerprint({
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_VERSION,
        "scheduled_age_ms": scheduled_age_ms,
    })
    economics = dict(economics or {})
    expected = economics.get("expected_out_atomic")
    # `floor_out_atomic` holds the amount the RESULT was scored at, which under v2 is the
    # expected amount less the measured haircut. The provider threshold stays recoverable
    # from `slippage_bps` and `quote_payload_json`; it is a signer bound, not a fill.
    scored = None
    return_bps = None
    if status == "executable":
        if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
            raise ValueError("an executable exit requires a positive expected_out_atomic")
        scored = _scored_out_atomic(expected)
        return_bps = _return_bps(scored, entry["quote_in_atomic"])
    elif status != "unavailable" or not failure_reason:
        raise ValueError("an unavailable exit requires a failure reason")
    values = {
        "observation_id": observation_id,
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_VERSION,
        "scheduled_age_ms": scheduled_age_ms,
        "status": status,
        "failure_reason": failure_reason,
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
        "route_binding_json": (
            json.dumps(economics.get("route_binding"), sort_keys=True)
            if economics.get("route_binding") is not None else None
        ),
        "quote_payload_json": (
            json.dumps(economics.get("quote_payload"), sort_keys=True)
            if economics.get("quote_payload") is not None else None
        ),
        "quote_started_at": economics.get("quote_started_at"),
        "quote_completed_at": economics.get("quote_completed_at"),
        "observed_at": observed_at,
        "return_bps": return_bps,
    }
    fingerprint = content_fingerprint(values)
    stored_values = {
        **values,
        "quote_in_atomic": _sqlite_atomic(values["quote_in_atomic"]),
        "quote_out_atomic": _sqlite_atomic(values["quote_out_atomic"]),
        "expected_out_atomic": _sqlite_atomic(values["expected_out_atomic"]),
        "floor_out_atomic": _sqlite_atomic(values["floor_out_atomic"]),
    }
    conn.execute(
        "INSERT INTO op_paper_exit_observations "
        f"({','.join(values)},row_fingerprint,inserted_at) "
        f"VALUES ({','.join('?' for _ in values)},?,?)",
        (*stored_values.values(), fingerprint, observed_at),
    )
    return observation_id


def append_bound_paper_completion(
    conn: sqlite3.Connection,
    *,
    request: Mapping[str, object],
    completion: PaperQuoteCompletion,
) -> bool:
    """Store one asynchronous result under the bucket that requested it."""
    request_id = str(request.get("request_id") or "")
    observation_id = str(request.get("observation_id") or "")
    try:
        scheduled_age_ms = int(request["scheduled_age_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("paper completion request identity is invalid") from exc
    expected_id = content_fingerprint({
        "request_id": request_id,
        "lifecycle_version": LIFECYCLE_VERSION,
        "scheduled_age_ms": scheduled_age_ms,
    })
    if (
        not request_id
        or observation_id != expected_id
        or completion.observation_id != observation_id
        or completion.venue_id != str(request.get("venue_id") or "")
    ):
        raise ValueError("paper completion request identity is invalid")
    existing = conn.execute(
        "SELECT observation_id FROM op_paper_exit_observations "
        "WHERE request_id=? AND lifecycle_version=? AND scheduled_age_ms=?",
        (request_id, LIFECYCLE_VERSION, scheduled_age_ms),
    ).fetchone()
    if existing is not None:
        if existing["observation_id"] != observation_id:
            raise ImmutableConflict("paper completion bucket identity changed")
        return False
    entry = conn.execute(
        "SELECT r.request_id,r.token_id,r.rule_version,r.venue_id,f.quote_out_decimals,"
        "f.expected_out_atomic,f.floor_out_atomic,f.quote_in_asset,f.quote_in_atomic,"
        "f.quote_in_decimals,f.receipt_at,f.route_binding_json "
        "FROM op_paper_fill_requests r JOIN op_paper_fills f USING(request_id) "
        "WHERE r.request_id=? AND f.status='filled'",
        (request_id,),
    ).fetchone()
    if entry is None or entry["venue_id"] != completion.venue_id:
        raise ValueError("paper completion entry is unavailable")
    if completion.status == "filled":
        if not isinstance(completion.economics, Mapping):
            raise ValueError("filled paper completion economics are invalid")
        _append_observation(
            conn,
            entry=entry,
            scheduled_age_ms=scheduled_age_ms,
            observed_at=completion.completed_at,
            status="executable",
            economics=completion.economics,
        )
    elif completion.status == "rejected" and completion.failure_reason:
        _append_observation(
            conn,
            entry=entry,
            scheduled_age_ms=scheduled_age_ms,
            observed_at=completion.completed_at,
            status="unavailable",
            failure_reason=completion.failure_reason,
        )
    else:
        raise ValueError("paper completion result is invalid")
    return True


def _append_outcome(
    conn: sqlite3.Connection,
    *,
    entry,
    status: str,
    result_code: str,
    observation_id: str | None,
    exit_floor_atomic: int | None,
    return_bps: int | None,
    outcome_at: str,
) -> None:
    outcome_id = content_fingerprint({
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_VERSION,
    })
    values = {
        "outcome_id": outcome_id,
        "request_id": entry["request_id"],
        "lifecycle_version": LIFECYCLE_VERSION,
        "status": status,
        "result_code": result_code,
        "terminal_observation_id": observation_id,
        "entry_quote_atomic": int(entry["quote_in_atomic"]),
        "exit_floor_atomic": exit_floor_atomic,
        "return_bps": return_bps,
        "outcome_at": outcome_at,
    }
    fingerprint = content_fingerprint(values)
    stored_values = {
        **values,
        "entry_quote_atomic": _sqlite_atomic(values["entry_quote_atomic"]),
        "exit_floor_atomic": _sqlite_atomic(values["exit_floor_atomic"]),
    }
    conn.execute(
        "INSERT INTO op_paper_outcomes "
        f"({','.join(values)},row_fingerprint,inserted_at) "
        f"VALUES ({','.join('?' for _ in values)},?,?)",
        (*stored_values.values(), fingerprint, outcome_at),
    )


def _result_code(scheduled_age_ms: int, return_bps: int) -> str | None:
    if scheduled_age_ms >= EXIT_AGE_MS:
        return "time_exit"
    if return_bps >= TARGET_BPS:
        return "target_first"
    if return_bps <= STOP_BPS:
        return "stop_first"
    return None


def _earliest_decision(conn: sqlite3.Connection, request_id: str, *, terminal: bool):
    """The FIRST bucket whose executable quote crosses a rail, or None.

    A quote can complete after its own 5 s bucket rolls over. On `pons_v1_4663` 100% of
    them did, a median of 46.8 s late, and on `pumpfun_solana` 15% did. So the bucket
    that is current at tick time is not the bucket that decides. Read the stored series
    in scheduled order instead.

    A missing bucket inside the series may still be in flight, and an earlier bucket can
    only decide EARLIER than a later one, so the scan stops at the gap and waits. At the
    terminal age nothing else can land, so the scan reads across the gap and answers from
    the observations that exist.

    COVERAGE LIMIT: buckets before the first observation are read as never requested, not
    as a gap. That is correct after a worker restart, which puts a live position back in
    the loop at whatever bucket the clock says, and those early buckets never can arrive.
    It is wrong in one case: the first requested bucket is still in flight while a later
    one already landed, so the scan answers from the later bucket. Closing that needs the
    requested ladder on disk, which no table holds today.
    """
    rows = conn.execute(
        "SELECT observation_id,scheduled_age_ms,status,floor_out_atomic,return_bps "
        "FROM op_paper_exit_observations "
        "WHERE request_id=? AND lifecycle_version=? ORDER BY scheduled_age_ms",
        (request_id, LIFECYCLE_VERSION),
    ).fetchall()
    expected = None
    for row in rows:
        bucket = int(row["scheduled_age_ms"])
        if expected is not None and bucket != expected and not terminal:
            return None
        expected = bucket + QUOTE_CADENCE_MS
        if row["status"] != "executable":
            continue
        return_bps = int(row["return_bps"])
        code = _result_code(bucket, return_bps)
        if code is not None:
            return row, code, return_bps
    return None


def drive_paper_lifecycle(
    conn: sqlite3.Connection,
    *,
    quote_fn,
    now_iso: str,
) -> dict[str, int]:
    """Quote the current bucket, then score the whole series for each open entry."""
    now = require_utc_iso(now_iso)
    activation = conn.execute(
        "SELECT activated_at FROM op_paper_lifecycle_activations "
        "WHERE lifecycle_version=?",
        (LIFECYCLE_VERSION,),
    ).fetchone()
    counters = {"eligible": 0, "quoted": 0, "completed": 0, "censored": 0, "extra": 0}
    if activation is None:
        return counters
    collect_floor = _shift_iso(now, -TERMINAL_AGE_MS)
    decided = {
        row["request_id"]
        for row in conn.execute(
            "SELECT request_id FROM op_paper_outcomes WHERE lifecycle_version=?",
            (LIFECYCLE_VERSION,),
        )
    }
    for entry in _active_entries(
        conn, activation["activated_at"], collect_newer_than_iso=collect_floor,
    ):
        # A decided position keeps producing observations for v3 to read. It must never
        # get a second outcome: `op_paper_outcomes` is immutable and v2's answer stands.
        already_decided = entry["request_id"] in decided
        counters["extra" if already_decided else "eligible"] += 1
        elapsed_ms = max(0, int((_parse(now) - _parse(entry["receipt_at"])).total_seconds() * 1000))
        if elapsed_ms < QUOTE_CADENCE_MS:
            continue
        scheduled_age_ms = min(
            (elapsed_ms // QUOTE_CADENCE_MS) * QUOTE_CADENCE_MS,
            TERMINAL_AGE_MS,
        )
        existing = conn.execute(
            "SELECT observation_id FROM op_paper_exit_observations "
            "WHERE request_id=? AND lifecycle_version=? AND scheduled_age_ms=?",
            (entry["request_id"], LIFECYCLE_VERSION, scheduled_age_ms),
        ).fetchone()
        observation_id = existing["observation_id"] if existing else None
        if existing is None:
            request = {
                "observation_id": content_fingerprint({
                    "request_id": entry["request_id"],
                    "lifecycle_version": LIFECYCLE_VERSION,
                    "scheduled_age_ms": scheduled_age_ms,
                }),
                "request_id": entry["request_id"],
                "token_id": entry["token_id"],
                # Sell what the entry quote said we get, not the signer's safety bound.
                "quantity_atomic": int(entry["expected_out_atomic"]),
                "token_decimals": int(entry["quote_out_decimals"]),
                "quote_asset": entry["quote_in_asset"],
                "quote_decimals": int(entry["quote_in_decimals"]),
                "venue_id": entry["venue_id"],
                "route_binding": json.loads(entry["route_binding_json"]),
                "scheduled_age_ms": scheduled_age_ms,
                "now_iso": now,
            }
            try:
                economics = quote_fn(request)
            except QuoteDeferred:
                # The bucket is in flight. `append_bound_paper_completion` stores it, and
                # the series scan below reads it on a later tick.
                pass
            except QuoteRejected as exc:
                observation_id = _append_observation(
                    conn,
                    entry=entry,
                    scheduled_age_ms=scheduled_age_ms,
                    observed_at=now,
                    status="unavailable",
                    failure_reason=str(exc) or "quote_unavailable",
                )
                counters["quoted"] += 1
            else:
                observation_id = _append_observation(
                    conn,
                    entry=entry,
                    scheduled_age_ms=scheduled_age_ms,
                    observed_at=now,
                    status="executable",
                    economics=economics,
                )
                counters["quoted"] += 1
        if already_decided:
            continue
        terminal = elapsed_ms >= TERMINAL_AGE_MS
        decision = _earliest_decision(conn, entry["request_id"], terminal=terminal)
        if decision is not None:
            row, result_code, return_bps = decision
            _append_outcome(
                conn,
                entry=entry,
                status="valid",
                result_code=result_code,
                observation_id=row["observation_id"],
                exit_floor_atomic=int(row["floor_out_atomic"]),
                return_bps=return_bps,
                outcome_at=now,
            )
            counters["completed"] += 1
        elif terminal:
            # 90 s passed and no stored quote crossed a rail. This is now the only path
            # to a censor, so the row means what its name says.
            _append_outcome(
                conn,
                entry=entry,
                status="censored",
                result_code="route_censored",
                observation_id=observation_id,
                exit_floor_atomic=None,
                return_bps=None,
                outcome_at=now,
            )
            counters["censored"] += 1
    return counters


def realistic_result_summary(
    conn: sqlite3.Connection,
    *,
    lifecycle_version: str = LIFECYCLE_VERSION,
) -> dict[str, object]:
    activation = conn.execute(
        "SELECT activated_at FROM op_paper_lifecycle_activations "
        "WHERE lifecycle_version=?",
        (lifecycle_version,),
    ).fetchone()
    base = {
        "lifecycle_version": lifecycle_version,
        "activated_at": activation["activated_at"] if activation else None,
        "valid_results": 0,
        "target_first": 0,
        "stop_first": 0,
        "time_exits": 0,
        "censored": 0,
        "open": 0,
        "by_venue": {
            venue: {
                "valid_results": 0,
                "average_net_return_bps": None,
                "target_first": 0,
                "stop_first": 0,
                "time_exits": 0,
                "censored": 0,
                "open": 0,
            }
            for venue in PAPER_RULE_BY_VENUE
        },
    }
    if activation is None:
        return base
    row = conn.execute(
        "SELECT SUM(status='valid') valid_results,"
        "SUM(result_code='target_first') target_first,"
        "SUM(result_code='stop_first') stop_first,"
        "SUM(result_code='time_exit') time_exits,"
        "SUM(status='censored') censored "
        "FROM op_paper_outcomes WHERE lifecycle_version=?",
        (lifecycle_version,),
    ).fetchone()
    open_count = len(_active_entries(
        conn, activation["activated_at"], lifecycle_version,
    ))
    # The arm and its matched control run the identical lifecycle, so the only honest
    # headline is the pair. A single pooled average would hide which one earned it.
    by_arm = {}
    for arm in conn.execute(
        "SELECT r.rule_version rule,SUM(o.status='valid') valid_results,"
        "ROUND(AVG(CASE WHEN o.status='valid' THEN o.return_bps END)) average_return,"
        "SUM(o.result_code='target_first') target_first,"
        "SUM(o.result_code='stop_first') stop_first,"
        "SUM(o.result_code='time_exit') time_exits,"
        "SUM(o.status='censored') censored "
        "FROM op_paper_outcomes o JOIN op_paper_fill_requests r USING(request_id) "
        "WHERE o.lifecycle_version=? GROUP BY 1",
        (lifecycle_version,),
    ):
        by_arm[arm["rule"]] = {
            "valid_results": int(arm["valid_results"] or 0),
            "average_net_return_bps": (
                int(arm["average_return"]) if arm["average_return"] is not None else None
            ),
            "target_first": int(arm["target_first"] or 0),
            "stop_first": int(arm["stop_first"] or 0),
            "time_exits": int(arm["time_exits"] or 0),
            "censored": int(arm["censored"] or 0),
        }
    by_venue = dict(base["by_venue"])
    for venue in PAPER_RULE_BY_VENUE:
        venue_row = conn.execute(
            "SELECT SUM(o.status='valid') valid_results,"
            "ROUND(AVG(CASE WHEN o.status='valid' THEN o.return_bps END)) average_return,"
            "SUM(o.result_code='target_first') target_first,"
            "SUM(o.result_code='stop_first') stop_first,"
            "SUM(o.result_code='time_exit') time_exits,"
            "SUM(o.status='censored') censored "
            "FROM op_paper_outcomes o JOIN op_paper_fill_requests r USING(request_id) "
            "WHERE o.lifecycle_version=? AND r.venue_id=?",
            (lifecycle_version, venue),
        ).fetchone()
        open_venue = conn.execute(
            "SELECT COUNT(*) FROM op_paper_fill_requests r "
            "JOIN op_paper_fills f USING(request_id) "
            "WHERE f.status='filled' AND f.receipt_at>=? AND r.venue_id=? "
            "AND NOT EXISTS (SELECT 1 FROM op_paper_outcomes o "
            "WHERE o.request_id=r.request_id AND o.lifecycle_version=?)",
            (activation["activated_at"], venue, lifecycle_version),
        ).fetchone()[0]
        by_venue[venue] = {
            "valid_results": int(venue_row["valid_results"] or 0),
            "average_net_return_bps": (
                int(venue_row["average_return"])
                if venue_row["average_return"] is not None else None
            ),
            "target_first": int(venue_row["target_first"] or 0),
            "stop_first": int(venue_row["stop_first"] or 0),
            "time_exits": int(venue_row["time_exits"] or 0),
            "censored": int(venue_row["censored"] or 0),
            "open": int(open_venue or 0),
        }
    return {
        **base,
        "valid_results": int(row["valid_results"] or 0),
        "target_first": int(row["target_first"] or 0),
        "stop_first": int(row["stop_first"] or 0),
        "time_exits": int(row["time_exits"] or 0),
        "censored": int(row["censored"] or 0),
        "open": open_count,
        "by_arm": by_arm,
        "by_venue": by_venue,
        "execution_haircut_bps_per_leg": EXECUTION_HAIRCUT_BPS,
    }

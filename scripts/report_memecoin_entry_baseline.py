#!/usr/bin/env python3
"""Read-only real-entry funnel, latency, and attribution report.

Trace-backed metrics are exact. Legacy evidence is emitted separately and never joined to a
"nearest" event, because that would manufacture attribution.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sniper.persistence.db_core import get_readonly_connection


DEFAULT_NETWORKS = ("solana", "1", "56", "8453", "4663")
DEFAULT_SOURCES = ("global_feed", "follow_activity")
STAGES = {
    "chain_resolution": "chain_resolution_ms",
    "market_data": "market_data_ms",
    "reservation": "reservation_ms",
    "safety_checks": "safety_checks_ms",
    "quote": "quote_ms",
    "signing": "signing_ms",
    "transaction_submitted": "transaction_submitted_ms",
    "chain_confirmation": "chain_confirmation_ms",
    "balance_delta": "balance_delta_ms",
    "persistence": "persistence_ms",
    "total": "total_ms",
}
COMPLETE_FILL_STAGES = tuple(STAGES.values())
OBSERVATION_COLUMNS = {
    "upstream_event_id", "normalized_side", "network_id", "mint", "observed_at",
    "was_curated", "was_real_approved", "clock_domain",
}
RECEIPT_TARGETS = {
    "receipt_to_decision": "decision_started_at",
    "receipt_to_submission": "transaction_submitted_at",
    "receipt_to_terminal": "completed_at",
}


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _finite(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _percentile(values, q):
    values = sorted(value for raw in values if (value := _finite(raw)) is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * q
    lo = math.floor(index)
    hi = math.ceil(index)
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def _distribution(values):
    clean = [value for raw in values if (value := _finite(raw)) is not None]
    return {
        "n": len(clean),
        "p50": _percentile(clean, 0.50),
        "p95": _percentile(clean, 0.95),
    }


def _pct(numerator, denominator):
    return round(100.0 * numerator / denominator, 2) if denominator else None


def _network(value, mint=None):
    value = str(value or "").strip().lower()
    if value == "1399811149":
        return "solana"
    if value:
        return value
    return "evm-unknown" if str(mint or "").strip().lower().startswith("0x") else "solana"


def _read_events(conn, tables, since):
    if "memecoin_events" not in tables:
        return []
    cols = _columns(conn, "memecoin_events")
    required = {"network_id", "mint", "side"}
    if not required <= cols:
        return []
    source = "signal_source" if "signal_source" in cols else "'global_feed'"
    action = "action" if "action" in cols else "NULL"
    stamp_columns = [name for name in ("received_at_normalized", "received_at", "created_at")
                     if name in cols]
    stamp = (f"COALESCE({','.join(stamp_columns)})" if len(stamp_columns) > 1
             else (stamp_columns[0] if stamp_columns else None))
    sql = (f"SELECT {source} AS signal_source, network_id, mint, side, {action} AS action "
           "FROM memecoin_events")
    params = []
    if since and stamp:
        sql += f" WHERE {stamp}>=?"
        params.append(since)
    return [dict(row) for row in conn.execute(sql, params)]


def _read_attempts(conn, tables, since):
    if "memecoin_entry_attempts" not in tables:
        return []
    cols = _columns(conn, "memecoin_entry_attempts")
    select = ",".join(sorted(cols))
    sql = f"SELECT {select} FROM memecoin_entry_attempts"
    params = []
    if since and "decision_started_at" in cols:
        sql += " WHERE decision_started_at>=?"
        params.append(since)
    return [dict(row) for row in conn.execute(sql, params)]


def _read_observations(conn, tables, since):
    if "memecoin_feed_observations_v2" not in tables:
        return [], False
    cols = _columns(conn, "memecoin_feed_observations_v2")
    if not OBSERVATION_COLUMNS <= cols:
        return [], False
    selected = ",".join(sorted(OBSERVATION_COLUMNS))
    sql = f"SELECT {selected} FROM memecoin_feed_observations_v2"
    params = []
    if since:
        sql += " WHERE observed_at>=?"
        params.append(since)
    rows = [dict(row) for row in conn.execute(sql, params)]
    for row in rows:
        row["signal_source"] = "global_feed"
    return rows, True


def _terminal_counts(attempts):
    return dict(sorted(Counter(
        str(row.get("terminal_reason") or "missing_terminal_reason") for row in attempts
    ).items()))


def _classify(attempts, events, trace_available, *, observations=None,
              eligibility_available=False, trace_origin_ids=None):
    reasons = [str(row.get("terminal_reason") or "").lower() for row in attempts]
    statuses = [str(row.get("terminal_status") or "").lower() for row in attempts]
    submitted = sum(row.get("transaction_submitted_at") is not None for row in attempts)
    fills = [row for row in attempts
             if str(row.get("terminal_status") or "").lower() == "filled"
             and row.get("fill_id") is not None]
    signal_rows = observations if eligibility_available else events
    buy_signals = [row for row in signal_rows
                   if str(row.get("normalized_side") or row.get("side") or "").lower() == "buy"]
    if eligibility_available:
        curated = [row for row in buy_signals if row.get("was_curated") == 1]
        eligible = [row for row in curated if row.get("was_real_approved") == 1]
        unknown_eligibility = sum(
            row.get("was_curated") is None
            or (row.get("was_curated") == 1 and row.get("was_real_approved") is None)
            for row in buy_signals
        )
        eligible_ids = {str(row.get("upstream_event_id")) for row in eligible
                        if row.get("upstream_event_id") is not None}
        attempt_ids = trace_origin_ids if trace_origin_ids is not None else {
            str(row.get("origin_event_id")) for row in attempts
            if row.get("origin_event_id") is not None
        }
        traced_eligible = len(eligible_ids & attempt_ids) if trace_available else None
    else:
        curated = eligible = None
        unknown_eligibility = None
        traced_eligible = None
    funnel = {
        "signals_observed": len(buy_signals),
        "curated_buys": len(curated) if curated is not None else None,
        "eligible_curated_buys": len(eligible) if eligible is not None else None,
        "point_in_time_eligibility_unknown_buys": unknown_eligibility,
        "eligible_buys_with_exact_trace": traced_eligible,
        "eligible_trace_coverage_pct": (
            _pct(traced_eligible, len(eligible))
            if traced_eligible is not None and eligible is not None else None
        ),
        "explicit_network_signals": sum(
            _network(row.get("network_id"), row.get("mint")) not in ("evm-unknown", "")
            for row in buy_signals),
        "unresolved_network_signals": sum(
            _network(row.get("network_id"), row.get("mint")) == "evm-unknown"
            for row in buy_signals),
        "reservations_accepted": (sum(row.get("pending_id") is not None for row in attempts)
                                  if trace_available else None),
        "reservations_refused": (sum(status == "refused" and row.get("pending_id") is None
                                     for status, row in zip(statuses, attempts))
                                 if trace_available else None),
        "route_failures": (sum(any(token in reason for token in (
            "unroutable", "unresolved_network", "ambiguous_network", "no venue", "no_context"
        )) for reason in reasons) if trace_available else None),
        "quote_failures": (sum(any(token in reason for token in (
            "quote_unavailable", "no quote", "no_tx"
        )) for reason in reasons) if trace_available else None),
        "safety_failures": (sum("safety" in reason for reason in reasons)
                            if trace_available else None),
        "duplicate_open_refusals": (sum(any(token in reason for token in (
            "duplicate", "already_open", "open_position", "lost_reservation_race",
            "one position per coin"
        )) for reason in reasons) if trace_available else None),
        "transactions_submitted": submitted if trace_available else None,
        "confirmed_fills": len(fills) if trace_available else None,
        "unknown_timeouts": (sum(status == "unknown" or "timeout" in reason
                                 for status, reason in zip(statuses, reasons))
                             if trace_available else None),
    }
    latency = {
        stage: _distribution(row.get(column) for row in attempts)
        for stage, column in STAGES.items()
    }
    valid_source_fills = [row for row in fills if (_finite(row.get("source_price")) or 0) > 0]
    exact = [row for row in valid_source_fills if (
        row.get("price_attribution") == "exact_event"
        and (_finite(row.get("quote_price")) or 0) > 0
        and (_finite(row.get("fill_price")) or 0) > 0
        and all(_finite(row.get(field)) is not None for field in (
            "source_drift_pct", "execution_slippage_pct", "total_copy_drift_pct")))
    ]
    complete_timing = [row for row in fills if all(
        _finite(row.get(field)) is not None for field in COMPLETE_FILL_STAGES)]
    attribution = {
        "confirmed_fills": len(fills),
        "valid_source_price_fills": len(valid_source_fills),
        "exact_event_drift_fills": len(exact),
        "exact_drift_coverage_pct": _pct(len(exact), len(valid_source_fills)),
        "complete_timing_fills": len(complete_timing),
        "complete_timing_pct": _pct(len(complete_timing), len(fills)),
        "source_drift_pct": _distribution(row.get("source_drift_pct") for row in fills),
        "execution_slippage_pct": _distribution(
            row.get("execution_slippage_pct") for row in fills),
        "total_copy_drift_pct": _distribution(row.get("total_copy_drift_pct") for row in fills),
    }
    return funnel, latency, attribution


def _parse_aware_timestamp(value):
    if value is None or not str(value).strip():
        return None, "missing_timestamp"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, "invalid_timestamp"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "naive_timestamp"
    return parsed.astimezone(timezone.utc), None


def _receipt_elapsed_ms(row, target_column):
    if str(row.get("clock_domain") or "") != "observed_real_utc":
        return None, "invalid_clock_domain"
    if row.get("observed_at") is None:
        return None, "missing_observed_at"
    if row.get(target_column) is None:
        return None, "stage_not_reached"
    observed, observed_error = _parse_aware_timestamp(row.get("observed_at"))
    if observed_error:
        return None, observed_error
    target, target_error = _parse_aware_timestamp(row.get(target_column))
    if target_error:
        return None, target_error
    elapsed_ms = (target - observed).total_seconds() * 1000.0
    if elapsed_ms < 0:
        return None, "negative_delta"
    return elapsed_ms, None


def _receipt_latency(attempts):
    result = {}
    for name, target_column in RECEIPT_TARGETS.items():
        values = []
        exclusions = Counter()
        for row in attempts:
            value, reason = _receipt_elapsed_ms(row, target_column)
            if reason:
                exclusions[reason] += 1
            else:
                values.append(value)
        result[name] = {
            **_distribution(values),
            "excluded_n": sum(exclusions.values()),
            "exclusion_reasons": dict(sorted(exclusions.items())),
        }
    result["clock_valid_coverage_pct"] = _pct(
        result["receipt_to_decision"]["n"], len(attempts),
    )
    return result


def _legacy_exec_latency(conn, tables, since):
    if "memecoin_exec_latency" not in tables:
        return []
    cols = _columns(conn, "memecoin_exec_latency")
    needed = {"network_id", "status", "gate_ms", "safety_ms", "exec_ms", "total_ms"}
    if not needed <= cols:
        return []
    stamp = "at" if "at" in cols else None
    sql = "SELECT network_id,status,gate_ms,safety_ms,exec_ms,total_ms FROM memecoin_exec_latency"
    params = []
    if since and stamp:
        sql += " WHERE at>=?"
        params.append(since)
    rows = [dict(row) for row in conn.execute(sql, params)]
    result = []
    for network in sorted({_network(row.get("network_id"), "") for row in rows}):
        group = [row for row in rows if _network(row.get("network_id"), "") == network]
        result.append({
            "network_id": network,
            "signal_source": "unknown_legacy",
            "statuses": dict(sorted(Counter(str(row.get("status") or "unknown")
                                             for row in group).items())),
            "latency_ms": {
                name: _distribution(row.get(column) for row in group)
                for name, column in (("gate", "gate_ms"), ("safety", "safety_ms"),
                                     ("execution", "exec_ms"), ("total", "total_ms"))
            },
        })
    return result


def _legacy_event_actions(events):
    grouped = {}
    for row in events:
        key = (_network(row.get("network_id"), row.get("mint")),
               str(row.get("signal_source") or "global_feed"))
        grouped.setdefault(key, Counter())[str(row.get("action") or "missing_action")] += 1
    return [
        {"network_id": network, "signal_source": source,
         "actions": dict(sorted(counts.items()))}
        for (network, source), counts in sorted(grouped.items())
    ]


def _legacy_pending(conn, tables, since):
    if "memecoin_pending_entries" not in tables:
        return []
    cols = _columns(conn, "memecoin_pending_entries")
    if not {"network_id", "mint", "status"} <= cols:
        return []
    source = "signal_source" if "signal_source" in cols else "'unknown_legacy'"
    reason = "resolution_reason" if "resolution_reason" in cols else "NULL"
    stamp = "reserved_at" if "reserved_at" in cols else None
    sql = (f"SELECT network_id,mint,{source} AS signal_source,status,"
           f"{reason} AS reason FROM memecoin_pending_entries")
    params = []
    if since and stamp:
        sql += f" WHERE {stamp}>=?"
        params.append(since)
    rows = [dict(row) for row in conn.execute(sql, params)]
    grouped = {}
    for row in rows:
        key = (_network(row.get("network_id"), row.get("mint")),
               str(row.get("signal_source") or "unknown_legacy"))
        bucket = grouped.setdefault(key, {"statuses": Counter(), "reasons": Counter()})
        bucket["statuses"][str(row.get("status") or "unknown")] += 1
        if row.get("reason"):
            bucket["reasons"][str(row["reason"])] += 1
    return [
        {"network_id": network, "signal_source": source,
         "reservations": sum(bucket["statuses"].values()),
         "statuses": dict(sorted(bucket["statuses"].items())),
         "terminal_reasons": dict(sorted(bucket["reasons"].items()))}
        for (network, source), bucket in sorted(grouped.items())
    ]


def _legacy_real_positions(conn, tables, since):
    if "memecoin_positions" not in tables:
        return []
    cols = _columns(conn, "memecoin_positions")
    if not {"network_id", "mint", "execution_mode"} <= cols:
        return []
    source = "signal_source" if "signal_source" in cols else "'unknown_legacy'"
    stamp = "opened_at" if "opened_at" in cols else None
    selected = ["network_id", "mint", f"{source} AS signal_source"]
    for column in ("their_price", "quote_price", "entry_price", "price_attribution",
                   "source_drift_pct", "execution_slippage_pct", "total_copy_drift_pct"):
        selected.append(column if column in cols else f"NULL AS {column}")
    sql = f"SELECT {','.join(selected)} FROM memecoin_positions WHERE execution_mode='real'"
    params = []
    if since and stamp:
        sql += f" AND {stamp}>=?"
        params.append(since)
    rows = [dict(row) for row in conn.execute(sql, params)]
    result = []
    keys = sorted({(_network(row.get("network_id"), row.get("mint")),
                    str(row.get("signal_source") or "unknown_legacy")) for row in rows})
    for network, source_name in keys:
        group = [row for row in rows if (
            _network(row.get("network_id"), row.get("mint")) == network
            and str(row.get("signal_source") or "unknown_legacy") == source_name)]
        valid_source = [row for row in group if (_finite(row.get("their_price")) or 0) > 0]
        exact = [row for row in valid_source if row.get("price_attribution") == "exact_event"
                 and (_finite(row.get("quote_price")) or 0) > 0
                 and (_finite(row.get("entry_price")) or 0) > 0]
        result.append({
            "network_id": network,
            "signal_source": source_name,
            "positions": len(group),
            "valid_source_price": len(valid_source),
            "exact_event_quote_fill": len(exact),
            "exact_drift_coverage_pct": _pct(len(exact), len(valid_source)),
        })
    return result


def build_entry_report(conn, since=None):
    """Build exact trace metrics plus explicitly separate legacy evidence; performs no writes."""
    tables = _tables(conn)
    trace_available = "memecoin_entry_attempts" in tables
    attempts = _read_attempts(conn, tables, since)
    events = _read_events(conn, tables, since)
    observations, observation_available = _read_observations(conn, tables, since)
    networks = list(DEFAULT_NETWORKS)
    for network in sorted({_network(row.get("network_id"), row.get("mint"))
                           for row in attempts + events + observations}):
        if network not in networks:
            networks.append(network)
    sources = list(DEFAULT_SOURCES)
    for source in sorted({str(row.get("signal_source") or "global_feed")
                          for row in attempts + events + observations}):
        if source not in sources:
            sources.append(source)
    rows = []
    for network in networks:
        for source in sources:
            source_trace_origin_ids = {
                str(row.get("origin_event_id")) for row in attempts
                if str(row.get("signal_source") or "global_feed") == source
                and row.get("origin_event_id") is not None
            }
            grouped_attempts = [row for row in attempts if (
                _network(row.get("network_id"), row.get("mint")) == network
                and str(row.get("signal_source") or "global_feed") == source)]
            grouped_events = [row for row in events if (
                _network(row.get("network_id"), row.get("mint")) == network
                and str(row.get("signal_source") or "global_feed") == source)]
            grouped_observations = [row for row in observations if (
                _network(row.get("network_id"), row.get("mint")) == network
                and str(row.get("signal_source") or "global_feed") == source)]
            exact_eligibility = observation_available and source == "global_feed"
            funnel, latency, attribution = _classify(
                grouped_attempts, grouped_events, trace_available,
                observations=grouped_observations, eligibility_available=exact_eligibility,
                trace_origin_ids=source_trace_origin_ids,
            )
            unknowns = []
            if source == "follow_activity":
                unknowns.append("point_in_time_eligibility_unavailable_for_follow_activity")
            elif not observation_available:
                unknowns.append("eligible_curated_buys_not_preserved_point_in_time")
            elif funnel["point_in_time_eligibility_unknown_buys"]:
                unknowns.append("point_in_time_eligibility_null_rows")
            if not trace_available:
                unknowns.append("attempt_trace_table_not_deployed")
            rows.append({
                "network_id": network,
                "signal_source": source,
                "attempts": len(grouped_attempts) if trace_available else None,
                "funnel": funnel,
                "terminal_reasons": (_terminal_counts(grouped_attempts)
                                     if trace_available else {}),
                "latency_ms": latency,
                "receipt_latency_ms": _receipt_latency(grouped_attempts),
                "attribution": attribution,
                "legacy_unknowns": unknowns,
            })
    return {
        "schema_version": 2,
        "since": since,
        "trace_available": trace_available,
        "rows": rows,
        "legacy_evidence": {
            "warning": "legacy rows are not nearest-event joined; source attribution may be unknown",
            "event_actions": _legacy_event_actions(events),
            "pending_reservations": _legacy_pending(conn, tables, since),
            "exec_latency": _legacy_exec_latency(conn, tables, since),
            "real_positions": _legacy_real_positions(conn, tables, since),
        },
    }


def _print_table(report):
    print("network\tsource\tsignals\teligible\tattempts\ttrace_pct\treserved\tsubmitted\t"
          "fills\tunknown\treceipt_decision_p50_ms\treceipt_decision_p95_ms\t"
          "decision_total_p50_ms\tdecision_total_p95_ms\texact_drift_pct")
    for row in report["rows"]:
        funnel = row["funnel"]
        total = row["latency_ms"]["total"]
        receipt = row["receipt_latency_ms"]["receipt_to_decision"]
        print("\t".join(str(value) for value in (
            row["network_id"], row["signal_source"], funnel["signals_observed"],
            funnel["eligible_curated_buys"], row["attempts"],
            funnel["eligible_trace_coverage_pct"], funnel["reservations_accepted"],
            funnel["transactions_submitted"], funnel["confirmed_fills"],
            funnel["unknown_timeouts"], receipt["p50"], receipt["p95"],
            total["p50"], total["p95"],
            row["attribution"]["exact_drift_coverage_pct"],
        )))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sniper_tracker.db")
    parser.add_argument("--since")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    conn = get_readonly_connection(str(Path(args.db).expanduser()))
    try:
        report = build_entry_report(conn, since=args.since)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        _print_table(report)


if __name__ == "__main__":
    main()

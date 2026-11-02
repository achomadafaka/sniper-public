#!/usr/bin/env python3
"""Evaluate a frozen organic BSC cohort; missing evidence cannot pass the speed goal."""
import argparse
from collections import Counter, defaultdict
import json
import math
import re
from datetime import datetime

TX = re.compile(r"0x[0-9a-f]{64}\Z")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _tx(value):
    value = str(value or "").lower()
    return value if TX.fullmatch(value) else None


def _utc_interval_ms(start, end):
    try:
        start, end = (datetime.fromisoformat(str(v).replace('Z', '+00:00')) for v in (start, end))
        return _number((end - start).total_seconds() * 1000) if start.tzinfo and end.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _distribution(values):
    values = sorted(value for value in values if value is not None)
    return {"n": len(values), "p50": values[math.ceil(len(values) * .5) - 1] if values else None,
            "p90": values[math.ceil(len(values) * .9) - 1] if values else None}


def _identity(row):
    wallet, mint = row.get("wallet"), row.get("mint")
    tx = _tx(row.get("source_tx"))
    upstream = row.get("upstream_id")
    if not wallet or not mint or not (tx or upstream):
        return None
    return ("tx" if tx else "upstream", tx or str(upstream),
            str(wallet).lower(), str(mint).lower())


def _witness_ms(row):
    if row.get("outcome") != "filled":
        return None
    tx = _tx(row.get("copy_tx"))
    block_hash = _tx(row.get("observed_hash"))
    clock = row.get("receipt_clock") or {}
    if str(row.get('chain_id')) == '4663':
        clock, _ = merge_rh_receipt_clocks(clock, None,
                                         conflict=row.get('receipt_clock_conflict', False))
        if clock is None:
            return None
    start, observed = clock.get("monotonic_ns"), row.get("observed_ns")
    block = row.get("observed_block")
    if row.get('chain_id') is not None and any(str(row.get(key)) != str(row['chain_id'])
            for key in ('observed_chain', 'confirmed_chain', 'booked_chain')):
        return None
    if (not tx or any(_tx(row.get(key)) != tx for key in
                      ("observed_tx", "confirmed_tx", "booked_tx"))
            or not block_hash or _tx(row.get("confirmed_hash")) != block_hash
            or _tx(row.get("booked_hash")) != block_hash
            or type(block) is not int or block < 1 or row.get("confirmed_block") != block
            or row.get("booked_block") != block
            or any(type(row.get(key)) is not int or row[key] != 1 for key in
                   ("observed_status", "confirmed_status", "booked_status"))
            or not clock.get("boot_id") or clock["boot_id"] != row.get("observed_boot_id")
            or type(start) is not int or type(observed) is not int or start < 0 or observed < start):
        return None
    return (observed - start) / 1_000_000


def merge_rh_receipt_clocks(first, second, *, conflict=False):
    """Union exact-origin RH clocks; incomparable boots stay unknown across later merges."""
    from sniper.util.receipt_clock import freeze_receipt_clock
    first, second = freeze_receipt_clock(first), freeze_receipt_clock(second)
    first = first if first and first['boot_id'] else None
    second = second if second and second['boot_id'] else None
    conflict = bool(conflict or (first and second and first['boot_id'] != second['boot_id']))
    return (None if conflict else min((first, second), key=lambda c: c['monotonic_ns'])
            if first and second else first or second), conflict


def rebase_receipt_latencies(row):
    """RH endpoints share the final original clock; never retain an interval from another anchor."""
    if str(row.get('chain_id')) != '4663':
        return row
    row = dict(row)
    clock, _ = merge_rh_receipt_clocks(row.get('receipt_clock'), None,
                                     conflict=row.get('receipt_clock_conflict', False))
    row['receipt_clock'] = clock
    for metric, prefix in (('dispatch_ms', 'dispatch'), ('ack_ms', 'ack')):
        endpoint, boot = row.get(prefix + '_monotonic_ns'), row.get(prefix + '_boot_id')
        row[metric] = None
        if (clock is not None and boot and boot == clock['boot_id']
                and type(endpoint) is int and endpoint >= clock['monotonic_ns']):
            row[metric] = (endpoint - clock['monotonic_ns']) / 1e6
    for metric, endpoint in (('signal_to_ack_ms', 'diagnostic_ack_completed_at'),
                             ('signal_to_confirmation_ms', 'diagnostic_confirmation_completed_at')):
        row['diagnostic_' + metric] = (_utc_interval_ms(clock['corrected_utc'], row.get(endpoint))
                                      if clock is not None else None)
    return row


def evaluate(rows, *, universe_complete=False, publication_losses=0):
    unique, duplicates, conflicts, unknown_identity = {}, 0, 0, 0
    for index, original in enumerate(rows):
        row = rebase_receipt_latencies(dict(original))
        key = _identity(row)
        if key is None:
            unknown_identity += 1
            key = ("unknown", index)
        if key in unique:
            duplicates += 1
            if any(unique[key].get(field) != row.get(field) for field in
                   ("eligible", "outcome", "stratum", "copy_tx", "confirmed_tx", "booked_tx",
                    "observed_tx", "observed_block", "observed_hash", "confirmed_block",
                    "confirmed_hash", "booked_block", "booked_hash", "receipt_clock",
                    "observed_boot_id", "observed_ns", "observed_status", "confirmed_status",
                    "booked_status", "dispatch_ms", "dispatch_boundary", "ack_ms")):
                conflicts += 1
            continue
        unique[key] = row
    eligible = [row for row in unique.values() if row.get("eligible") is True]
    unknown = sum(row.get("eligible") is not True and row.get("eligible") is not False
                  for row in unique.values())
    refused = sum(row.get("eligible") is False for row in unique.values())
    overlap = sum(_tx(row.get("source_tx")) is None and bool(row.get("upstream_id"))
                  for row in unique.values())
    groups = defaultdict(list)
    for row in eligible:
        groups[str(row.get("stratum") or "unknown")].append(row)
    strata = {}
    verified = sub200 = filled = 0
    for name, members in groups.items():
        witnesses = [_witness_ms(row) for row in members]
        count = sum(value is not None for value in witnesses)
        successes = sum(value is not None and value <= 200 for value in witnesses)
        fills = sum(row.get("outcome") == "filled" for row in members)
        verified += count
        sub200 += successes
        filled += fills
        strata[name] = {"eligible": len(members), "outcomes": dict(Counter(
            row.get("outcome") or "unknown" for row in members)), "fill_ms": _distribution(witnesses),
            "blocks_behind": _distribution([row['landing_block'] - row['source_block']
                for row in members if type(row.get('source_block')) is int
                and type(row.get('landing_block')) is int
                and 0 < row['source_block'] <= row['landing_block']]),
            "dispatch_ms": _distribution([_number(row.get("dispatch_ms")) for row in members
                                           if row.get("dispatch_boundary") == "socket_write"]),
            "sender_dispatch_ms": _distribution([_number(row.get("dispatch_ms")) for row in members]),
            "ack_ms": _distribution([_number(row.get("ack_ms")) for row in members]),
            "verified_sub200": successes, "verified_sub200_fraction": successes / len(members)}
    fraction = sub200 / len(eligible) if eligible else None
    coverage = verified / filled if filled else None
    blockers = []
    for reason, blocked in (
        ("universe_incomplete", universe_complete is not True),
        ("unknown_route_readiness", any(name not in {"native_warm", "native_cold", "shared", 'shared_warm', 'shared_cold'}
                                        for name in strata)),
        ("publication_loss", publication_losses != 0),
        ("unknown_identity", unknown_identity > 0), ("duplicate_conflict", conflicts > 0),
        ("unknown_eligibility", unknown > 0), ("cross_feed_overlap_unknown", overlap > 0),
        ("eligible_below_100", len(eligible) < 100),
        ("confirmed_witness_coverage_below_95pct", coverage is None or coverage < .95),
        ("all_eligible_sub200_below_90pct", fraction is None or fraction < .9)):
        if blocked:
            blockers.append(reason)
    for name, stats in strata.items():
        if stats["fill_ms"]["n"] < 30:
            blockers.append("witnesses_below_30:" + name)
    warm = strata.get("native_warm")
    if warm:
        for metric, p50, p90, reason in (
                ("dispatch_ms", 20, 40, "native_dispatch_target_unmet"),
                ("ack_ms", 35, 60, "native_ack_target_unmet")):
            stats = warm[metric]
            if stats["n"] < 30 or stats["p50"] > p50 or stats["p90"] > p90:
                blockers.append(reason)
    return {"received_unique": len(unique), "duplicates": duplicates, "eligible": len(eligible),
            'utc_diagnostics': {name: _distribution([_number(row.get('diagnostic_' + name))
                for row in unique.values()]) for name in ('signal_to_confirmation_ms', 'signal_to_ack_ms')},
            "received_reasons": dict(Counter(row.get('reason') or 'unknown' for row in unique.values())),
            "unknown_eligibility_reasons": dict(Counter(row.get('reason') or 'unknown'
                for row in unique.values() if row.get('eligible') is not True and row.get('eligible') is not False)),
            "source_feeds": dict(Counter(row.get('source_feed') or 'unknown' for row in unique.values())),
            "venues": dict(Counter(row.get('venue') or 'unknown' for row in unique.values())),
            "policy_refusals": refused, "unknown_eligibility": unknown,
            "publication_losses": publication_losses, "verified_witnesses": verified,
            "confirmed_fills": filled, "confirmed_witness_coverage": coverage,
            "verified_sub200": sub200, "verified_sub200_fraction": fraction,
            "strata": strata, "blockers": blockers, "goal_proved": not blockers}


def read_cohort(ledger, timing, release, *, limit=10000, chain='56', source_records=(),
                runner_generation=None):
    """Bounded indexed reads only. Missing producer/decode coverage keeps the universe incomplete."""
    from sniper.persistence.db_memecoin import entry_fill_for_attempt
    from sniper.services.memecoin_fill_timing import one, source_ref, facts
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise ValueError("full release SHA required")
    chain = str(chain)
    if chain not in {'56', '4663'}:
        raise ValueError('explicit supported chain required')
    signals = {}
    events = timing.execute("SELECT record_json FROM memecoin_execution_observer_events "
        "WHERE generation>=? AND generation<? ORDER BY generation,sequence LIMIT ?",
        (release + ":", release + ";", limit + 1)).fetchall()
    accepted = []
    for (raw,) in events[:limit]:
        event = json.loads(raw)
        if (str(event.get('chain_id', '56')) == chain and
                (runner_generation is None or event.get('runner_generation') == runner_generation)):
            accepted.append(event)
    for event in list(source_records)[:limit] + accepted:
        if event.get("kind") not in {"signal", "outcome"} or not event.get("signal_id"):
            continue
        key = event["signal_id"], event.get("source_user_id"), event.get("mint")
        previous = signals.get(key, {})
        merged = {**previous, **event}
        if chain == '4663':
            clock, merged['receipt_clock_conflict'] = merge_rh_receipt_clocks(
                previous.get('receipt_clock'), event.get('receipt_clock'),
                conflict=previous.get('receipt_clock_conflict') or event.get('receipt_clock_conflict'))
        else:
            clock = previous.get("receipt_clock") or event.get("receipt_clock")
            if clock and event.get("receipt_clock"):
                candidate = event["receipt_clock"]
                if clock.get("boot_id") == candidate.get("boot_id"):
                    clock = min((clock, candidate), key=lambda c: c["monotonic_ns"])
        signals[key] = {**merged, "receipt_clock": clock}
    rows = []
    for (signal_id, uid, mint), signal in signals.items():
        source_tx = _tx(source_ref({"origin_event_id": signal_id, "network_id": chain}))
        row = {"source_tx": source_tx, "upstream_id": None if source_tx else signal_id,
               "wallet": uid, "mint": mint, "eligible": signal.get("eligibility"),
               "outcome": signal.get("status") or "unknown", "receipt_clock": signal.get("receipt_clock"),
               "stratum": "unknown", "reason": signal.get("reason"), 'source_feed': signal.get('source_feed')}
        row['chain_id'] = chain
        if chain == '4663':
            row['receipt_clock_conflict'] = signal.get('receipt_clock_conflict', False)
        row['source_only'] = signal.get('source_only', False)
        row['attempt_id'] = signal.get('attempt_id')
        attempt = one(ledger, "SELECT * FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_attempt_origin "
            "WHERE origin_event_id=? AND network_id=? AND source_user_id=? AND mint=? "
            "ORDER BY decision_started_at DESC LIMIT 1", (signal_id, chain, uid, mint))
        if attempt:
            row["outcome"] = attempt.get("terminal_status") or "unknown"
            row['reason'] = attempt.get('terminal_reason') or row['reason']
            row["copy_tx"] = attempt.get("tx_sig")
            row['attempt_id'] = attempt['attempt_id']
            if chain == '4663' and row['outcome'] == 'filled':
                from sniper.services.memecoin_bsc_execution_observation import confirmed_real_entry_fill
                if confirmed_real_entry_fill(ledger, attempt['attempt_id'], attempt.get('tx_sig'), chain=chain):
                    # A canonically booked real entry proves funded admission, even if publication
                    # was missing. Its execution latency still needs the separate exact witness.
                    row['eligible'] = True
                    row['actual_funded_fill'] = True
            row['attempt_release_bound'] = False
            held = facts(attempt, timing=one(timing,
                'SELECT * FROM memecoin_fill_timing WHERE attempt_id=?', (attempt['attempt_id'],)))
            row['source_block'], row['landing_block'] = held['source_slot'], held['landing_slot']
            trace = json.loads(attempt.get("trace_json") or "{}")
            diagnostic_start = attempt.get('observed_at') if attempt.get('clock_domain') == 'observed_real_utc' else None
            for stage in trace.get('submission_events') or ():
                if stage.get('stage') == 'transaction_submitted':
                    row['diagnostic_signal_to_ack_ms'] = _utc_interval_ms(diagnostic_start, stage.get('completed_at'))
                    if chain == '4663':
                        row['diagnostic_ack_completed_at'] = stage.get('completed_at')
                elif stage.get('stage') == 'chain_confirmation':
                    row['diagnostic_signal_to_confirmation_ms'] = _utc_interval_ms(diagnostic_start, stage.get('completed_at'))
                    if chain == '4663':
                        row['diagnostic_confirmation_completed_at'] = stage.get('completed_at')
            if chain == '4663':
                row['receipt_clock'], row['receipt_clock_conflict'] = merge_rh_receipt_clocks(
                    row['receipt_clock'], trace.get('receipt_clock'),
                    conflict=row['receipt_clock_conflict'])
            else:
                row["receipt_clock"] = row["receipt_clock"] or trace.get("receipt_clock")
            witness = one(timing, "SELECT * FROM memecoin_execution_observations "
                "WHERE attempt_id=? AND tx_hash=? AND generation>=? AND generation<? "
                "ORDER BY generation DESC LIMIT 1",
                (attempt["attempt_id"], str(attempt.get("tx_sig") or "").lower(), release + ":", release + ";"))
            if witness:
                dispatch = json.loads(witness.get("dispatch_json") or "{}")
                if (dispatch.get('chain_id') == chain and dispatch.get('attempt_id') == attempt['attempt_id']
                        and _tx(dispatch.get('tx_hash')) == _tx(attempt.get('tx_sig'))):
                    row['attempt_release_bound'] = True
                    if chain == '4663':
                        row['eligible'] = True  # Signing/dispatch follows the unchanged admission gates.
                row['venue'] = dispatch.get('venue')
                row["dispatch_boundary"] = dispatch.get("boundary")
                if dispatch.get("route_lane") == "native":
                    ready = dispatch.get("ready_at_original_ingress")
                    row["stratum"] = "native_warm" if ready is True else "native_cold" if ready is False else "native_unknown"
                elif dispatch.get("route_lane") == "shared":
                    ready = dispatch.get('ready_at_original_ingress')
                    row['stratum'] = ('shared' if chain == '56' else 'shared_warm' if ready is True
                                      else 'shared_cold' if ready is False else 'shared_unknown')
                row['entry_preparation'] = dispatch.get('entry_preparation')
                if chain == '4663':
                    row['dispatch_monotonic_ns'] = dispatch.get('emitted_ns')
                    row['dispatch_boot_id'] = dispatch.get('boot_id')
                clock = row["receipt_clock"] or {}
                start = clock.get("monotonic_ns")
                if (type(start) is int and dispatch.get("boot_id") == clock.get("boot_id")
                        and type(dispatch.get("emitted_ns")) is int and dispatch["emitted_ns"] >= start):
                    row["dispatch_ms"] = (dispatch["emitted_ns"] - start) / 1e6
                success = json.loads(witness.get("first_success_json") or "{}")
                booking = json.loads(witness.get("booking_json") or "{}")
                receipt = booking.get("receipt") or {}
                fill = entry_fill_for_attempt(ledger, attempt["attempt_id"], attempt.get("tx_sig"))
                real_fill = bool(fill)
                if fill and chain == '4663':
                    from sniper.services.memecoin_bsc_execution_observation import confirmed_real_entry_fill
                    real_fill = bool(confirmed_real_entry_fill(ledger, attempt['attempt_id'],
                                                              attempt.get('tx_sig'), chain=chain))
                identity = (chain == '56' or (dispatch.get('chain_id') == chain
                            and success.get('chain_id') == chain and booking.get('chain_id') == chain
                            and booking.get('position_id') == (fill or {}).get('position_id')))
                if witness.get("validation_state") == "verified" and real_fill and identity:
                    row['landing_block'] = success.get('block_number')
                    row.update(observed_tx=success.get("tx_hash"), observed_ns=success.get("observed_ns"),
                        observed_boot_id=success.get("boot_id"), observed_block=success.get("block_number"),
                        observed_hash=success.get("block_hash"), observed_status=1,
                        confirmed_tx=receipt.get("transactionHash"), confirmed_hash=receipt.get("blockHash"),
                        confirmed_block=int(str(receipt.get("blockNumber")), 0), confirmed_status=1,
                        booked_tx=attempt["tx_sig"], booked_hash=receipt.get("blockHash"),
                        booked_block=int(str(receipt.get("blockNumber")), 0), booked_status=1,
                        observed_chain=chain, confirmed_chain=chain, booked_chain=chain,
                        observation_kind=success.get('observation_kind'))
            for stage in trace.get("submission_events", []):
                if stage.get("stage") == "transaction_submitted":
                    if chain == '4663':
                        row['ack_monotonic_ns'] = stage.get('monotonic_ns')
                        row['ack_boot_id'] = stage.get('boot_id')
                    elif stage.get('receipt_elapsed_ms') is not None:
                        row["ack_ms"] = stage["receipt_elapsed_ms"]
        rows.append(rebase_receipt_latencies(row))
    prefix = '' if chain == '56' else chain + ':'
    losses = sum(count for name, count in timing.execute(
        "SELECT name,count FROM memecoin_execution_observer_stats")
        if name in {prefix + n for n in ('sequence_gaps', 'invalid_admission', 'ipc_refused',
                                        'head_sequence_gaps', 'invalid_observation', 'wrong_chain')})
    publisher_losses = {}
    for event in accepted:
        generation = event.get('generation')
        publisher_losses[generation] = max(publisher_losses.get(generation, 0),
            int(event.get('overwritten') or 0) + int(event.get('ipc_failures') or 0))
    # The last serialized event can itself be lost. Current-generation publisher health retains
    # that loss even when no later event arrives to carry its counters.
    for (raw,) in timing.execute('SELECT state_json FROM memecoin_execution_observer_health '
                                 'WHERE owner>=? AND owner<?',
                                 ('publisher:' + chain + ':', 'publisher:' + chain + ';')):
        state = json.loads(raw)
        generation = state.get('generation')
        if (state.get('chain_id') == chain and isinstance(generation, str)
                and generation.startswith(release + ':')
                and (runner_generation is None or generation == runner_generation)):
            key = generation + ':' + str(state.get('producer_id'))
            publisher_losses[key] = max(publisher_losses.get(key, 0),
                int(state.get('overwritten') or 0) + int(state.get('ipc_failures') or 0))
    losses += sum(publisher_losses.values()) + sum(event.get('kind') == 'gap' for event in accepted)
    return {"metadata": {"release": release, "organic": True, "universe_complete": False,
                         'chain_id': chain, 'runner_generation': runner_generation,
                         'generations': sorted({e['generation'] for e in accepted if e.get('generation')}),
                         'observation_kind': ('confirmation_observation' if chain == '4663' else
                                              'head_receipt_observation'),
                         'source_records_truncated': len(source_records) > limit,
                         "publication_losses": losses, "truncated": len(events) > limit,
                         "gap": "raw decode drops and eligibility before all policy gates remain unknown"},
            "rows": rows}


def main():
    from pathlib import Path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", nargs="?", help="Frozen JSON with rows and complete-universe/loss metadata")
    parser.add_argument("--ledger")
    parser.add_argument("--timing")
    parser.add_argument("--release")
    args = parser.parse_args()
    if args.cohort:
        with open(args.cohort) as source:
            document = json.load(source)
    else:
        import sqlite3
        if not all((args.ledger, args.timing, args.release)):
            parser.error("cohort JSON or --ledger --timing --release required")
        from sniper.persistence.db_core import get_readonly_connection
        ledger = get_readonly_connection(args.ledger)
        timing = sqlite3.connect("file:" + args.timing + "?mode=ro", uri=True)
        timing.row_factory = sqlite3.Row
        try:
            document = read_cohort(ledger, timing, args.release)
        finally:
            ledger.close()
            timing.close()
    metadata = document.get("metadata") or {}
    report = evaluate(document["rows"], universe_complete=metadata.get("universe_complete") is True
                      and metadata.get("organic") is True,
                      publication_losses=metadata.get("publication_losses"))
    print(json.dumps({"metadata": metadata, **report}, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

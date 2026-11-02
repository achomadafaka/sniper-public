#!/usr/bin/env python3
"""Read-only RH original-signal→successful-confirmation-observation upper-bound report.

Reuses the BSC evaluator. Source rows without a runner handoff/clock remain in the denominator;
their eligibility and timing remain unknown. This script never polls an endpoint or books a fill.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.research import bsc_entry_speed_report as shared
from sniper.util.receipt_clock import freeze_receipt_clock


def _utc(value):
    at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('aware UTC cohort boundaries required')
    return at.astimezone(timezone.utc).isoformat()


def _json(value):
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def read_cohort(ledger, timing, release, *, since, until, source=None, limit=10000,
                runner_generation=None):
    since, until = _utc(since), _utc(until)
    if since >= until or not 1 <= limit <= 10000:
        raise ValueError('positive bounded UTC cohort required')
    records, gaps, counts = [], [], {}

    def add(signal_id, uid, mint, clock, feed, reason, **extra):
        records.append(dict(kind='signal', chain_id='4663', signal_id=signal_id,
                            source_user_id=uid, mint=mint, receipt_clock=freeze_receipt_clock(clock),
                            source_feed=feed, reason=reason, eligibility=None, status='unknown',
                            source_only=True, **extra))

    # This chain/id index gives a bounded recent page. A tail that does not reach the requested
    # boundary is explicitly censored; never scan the production ledger to make it look complete.
    try:
        cursor = ledger.execute('SELECT signature,source_user_id,mint,detected_at,source_valuation_json '
            'FROM onchain_wallet_detections INDEXED BY idx_mc_onchain_detect_network_id '
            "WHERE network_id='4663' ORDER BY id DESC LIMIT ?", (limit + 1,))
        recent = cursor.fetchall()
        counts['watcher_recent_page'] = len(recent[:limit])
        selected = 0
        for row in recent[:limit]:
            signature, uid, mint, at, valuation = tuple(row)
            if since <= _utc(at) < until:
                add('onchain:' + signature, uid, mint, _json(valuation).get('receipt_clock'),
                    'onchain_watch', 'source_without_measured_handoff')
                selected += 1
        counts['watcher_received'] = selected
        if len(recent) > limit and _utc(tuple(recent[limit - 1])[3]) >= since:
            gaps.append('watcher_page_truncated')
    except (sqlite3.Error, ValueError, TypeError):
        gaps.append('watcher_source_unavailable')

    try:
        raw = ledger.execute('SELECT upstream_event_id,source_user_id,mint,raw_json FROM '
            'memecoin_feed_observations_v2 INDEXED BY idx_mc_feed_v2_observed_at '
            "WHERE observed_at>=? AND observed_at<? AND network_id='4663' AND normalized_side='buy' "
            'ORDER BY observed_at LIMIT ?', (since, until, limit + 1)).fetchall()
        counts['independent_feed_received'] = len(raw[:limit])
        for oid, uid, mint, payload in raw[:limit]:
            parsed = _json(payload)
            add(oid, uid, mint, parsed.get('receipt_clock'), parsed.get('signal_source') or 'independent_feed',
                'source_without_measured_handoff')
        if len(raw) > limit:
            gaps.append('independent_feed_page_truncated')
    except sqlite3.Error:
        gaps.append('independent_feed_source_unavailable')

    source_losses, heartbeat_window = None, None
    if source is None:
        gaps.append('sequencer_source_unavailable')
    else:
        try:
            columns = {row[1] for row in source.execute('PRAGMA table_info(rh_feed_signals)')}
            anchor = 'receipt_clock_json' if 'receipt_clock_json' in columns else 'NULL'
            raw = source.execute('SELECT tx_hash,source_user_id,wallet,mint,signal,reason,' + anchor +
                ' FROM rh_feed_signals INDEXED BY idx_rh_feed_signals_arrival '
                'WHERE arrival_utc>=? AND arrival_utc<? ORDER BY arrival_utc,id LIMIT ?',
                (since, until, limit + 1)).fetchall()
            counts['sequencer_candidates'] = len(raw[:limit])
            for tx, uid, wallet, mint, signal, reason, clock_json in raw[:limit]:
                if not signal:
                    continue  # Authenticity rejects are coverage facts, never funded-buy signals.
                final_reason = 'source_signal_without_decision'
                try:
                    decision = source.execute('SELECT reason FROM rh_fast_decisions '
                        'INDEXED BY idx_rh_fast_decisions_tx WHERE tx_hash=? AND mint=? AND wallet=? '
                        'ORDER BY id DESC LIMIT 1', (tx, mint, wallet)).fetchone()
                    if decision:
                        final_reason = decision[0]
                except sqlite3.Error:
                    gaps.append('decision_source_unavailable')
                add('rhfast:4663:' + str(tx).lower() + ':' + mint, uid or wallet, mint,
                    _json(clock_json), 'rh_sequencer', final_reason)
            counts['sequencer_received'] = sum(r['source_feed'] == 'rh_sequencer' for r in records)
            if len(raw) > limit:
                gaps.append('sequencer_page_truncated')
            heartbeat_limit = min(limit, 1000)
            heartbeats = source.execute('SELECT at,queue_drops,seq_gaps,bad_frames,frame_errors '
                'FROM rh_feed_heartbeat ORDER BY rowid DESC LIMIT ?', (heartbeat_limit,)).fetchall()
            heartbeat_window = dict(examined=len(heartbeats), selected=0, limit=heartbeat_limit,
                                    boundary_known=False, counter_resets=0, losses_exact=False)
            previous, maxima, malformed = None, [], False
            for heartbeat in reversed(heartbeats):
                try:
                    at = _utc(heartbeat[0])
                except (ValueError, TypeError):
                    malformed = True
                    continue
                if at >= until:
                    continue
                counters = tuple(heartbeat[1:])
                valid = all(type(value) is int and value >= 0 for value in counters)
                if at <= since:
                    heartbeat_window['boundary_known'] = True
                if at < since:
                    previous = counters if valid else None
                    continue
                heartbeat_window['selected'] += 1
                if not valid:
                    malformed = True
                    previous = None
                    continue
                if previous and any(now < old for now, old in zip(counters, previous)):
                    heartbeat_window['counter_resets'] += 1
                maxima.append(sum(counters))
                previous = counters
            heartbeat_window['observed_cumulative_max'] = max(maxima) if maxima else None
            # A latest zero cannot erase an earlier gap; unidentified process resets prevent exact
            # cohort subtraction. Keep the observed maximum and explicit coverage uncertainty.
            source_losses = None if malformed else heartbeat_window['observed_cumulative_max']
            if malformed:
                gaps.append('sequencer_coverage_heartbeat_malformed')
            if heartbeat_window['counter_resets']:
                gaps.append('sequencer_coverage_counter_reset')
            if not heartbeat_window['boundary_known']:
                gaps.append('sequencer_coverage_heartbeat_window_truncated' if
                    len(heartbeats) == heartbeat_limit else 'sequencer_coverage_heartbeat_boundary_unknown')
            if not heartbeat_window['selected']:
                gaps.append('sequencer_coverage_heartbeat_unknown')
        except (sqlite3.Error, ValueError, TypeError):
            gaps.append('sequencer_source_schema_unavailable')

    document = shared.read_cohort(ledger, timing, release, chain='4663', source_records=records,
                                  runner_generation=runner_generation, limit=limit)
    # Different transports for the exact same wallet/tx/mint are deliveries, not new buys. Keep the
    # measured attempt and earliest same-boot T0; contradictory funded hashes remain conflicts.
    unique, unknown = {}, []
    for row in document['rows']:
        anchor, conflict = shared.merge_rh_receipt_clocks(row.get('receipt_clock'), None,
            conflict=row.get('receipt_clock_conflict', False))
        row['receipt_clock'] = anchor
        row['receipt_clock_conflict'] = conflict
        key = shared._identity(row)
        if key is None:
            unknown.append(row)
            continue
        previous = unique.get(key)
        if previous is None:
            unique[key] = row
            continue
        if previous.get('copy_tx') and row.get('copy_tx') and previous['copy_tx'] != row['copy_tx']:
            unknown.append(row)  # Let the common evaluator block the conflicting exact origin.
            continue
        chosen = row if row.get('attempt_id') and not previous.get('attempt_id') else previous
        chosen['receipt_clock'], chosen['receipt_clock_conflict'] = shared.merge_rh_receipt_clocks(
            previous.get('receipt_clock'), row.get('receipt_clock'),
            conflict=previous.get('receipt_clock_conflict') or row.get('receipt_clock_conflict'))
        chosen['deliveries'] = previous.get('deliveries', 1) + 1
        unique[key] = chosen
    document['rows'] = []
    for row in list(unique.values()) + unknown:
        row = shared.rebase_receipt_latencies(row)
        anchor = row['receipt_clock']
        if anchor and not since <= _utc(anchor['corrected_utc']) < until:
            continue
        document['rows'].append(row)
    metadata = document['metadata']
    if metadata.get('source_records_truncated'):
        gaps.append('source_fan_in_truncated')
    metadata.update(since_utc=since, until_utc=until, source_counts=counts,
                    source_clock_unknown=sum(not r.get('receipt_clock') for r in document['rows']),
                    source_coverage_gaps=sorted(set(gaps)), source_publication_losses=source_losses,
                    source_heartbeat_window=heartbeat_window,
                    attempt_release_unknown=sum(bool(r.get('attempt_id')) and
                        not r.get('attempt_release_bound') for r in document['rows']),
                    upper_bound=True, processed_execution_observed=False)
    if source_losses is not None:
        metadata['publication_losses'] += source_losses
    metadata['universe_complete'] = False
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--timing', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--since', required=True)
    parser.add_argument('--until', required=True)
    parser.add_argument('--source', help='Optional existing RH feed/decision database')
    parser.add_argument('--runner-generation')
    args = parser.parse_args()
    from sniper.persistence.db_core import get_readonly_connection
    ledger = get_readonly_connection(args.ledger)
    timing = sqlite3.connect('file:' + args.timing + '?mode=ro', uri=True, timeout=1)
    source = (sqlite3.connect('file:' + args.source + '?mode=ro', uri=True, timeout=1)
              if args.source else None)
    try:
        document = read_cohort(ledger, timing, args.release, since=args.since, until=args.until,
                              source=source, runner_generation=args.runner_generation)
        report = shared.evaluate(document['rows'], universe_complete=False,
                                 publication_losses=document['metadata']['publication_losses'])
        print(json.dumps(dict(metadata=document['metadata'], **report), indent=2, sort_keys=True))
    finally:
        ledger.close()
        timing.close()
        if source is not None:
            source.close()


if __name__ == '__main__':
    main()

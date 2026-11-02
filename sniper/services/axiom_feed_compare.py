"""Compare live Axiom and J7 receipts without provider clocks or trade decisions."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone

WINDOW_S = 120.0
AMOUNT_TOLERANCE = 0.05
SIGNATURE_COLUMNS = ('tx_sig', 'signature', 'tx_hash')
MATCH_KINDS = ('exact', 'weak', 'weak_no_amount')


def compare_feeds(conn, *, chain=None, since=None, until=None, window_s=WINDOW_S):
    """Return observations from one receipt interval. Coverage remains unproven."""
    if not math.isfinite(window_s) or window_s <= 0:
        raise ValueError('window_s must be positive and finite')
    _window_sql('received_at', since, until)
    if since is not None and until is not None and _epoch(since) >= _epoch(until):
        raise ValueError('since must precede until')
    from sniper.services.axiom_address_verify import watcher_address_owners
    owners = {(str(c), _address(c, address)): owner
              for (c, address), owner in watcher_address_owners(conn).items()}
    cohort = {(c, str(owner[0])) for (c, _), owner in owners.items()}
    signature_column = _signature_column(conn)
    axiom, ax_limits = _axiom_rows(conn, owners, chain, since, until)
    j7, j7_limits, unobserved = _j7_rows(conn, cohort, chain, since, until, signature_column)
    chains = {}
    for key in sorted(set(axiom) | set(j7) | set(unobserved)):
        chains[key] = _compare_one(axiom.get(key, []), j7.get(key, []), window_s)
        chains[key]['unobserved_j7'] = unobserved.get(key, 0)
    return {
        'chains': chains,
        'exact_matching': 'receipt identity; legacy event signature ' + (signature_column or 'unavailable'),
        'unattributed_axiom_rows': ax_limits['unattributed'],
        'excluded_axiom_rows': ax_limits['excluded'],
        'unattributed_j7_rows': j7_limits['unattributed'],
        'unmatchable_axiom_rows': ax_limits['unmatchable'],
        'unmatchable_j7_rows': j7_limits['unmatchable'],
        'excluded_axiom_provenance': dict(ax_limits['provenance']),
        'receipt_window': {'since': since, 'until_exclusive': until},
        'window_s': window_s,
        'coverage_verdict': 'unproven: subscription intervals unavailable',
        'superiority_verdict': 'unproven',
        'minimum_observation_days': 7,
        'observation_limits': [
            'Only explicit live WebSocket Axiom rows enter the comparison.',
            'The cohort contains verified chain and trader identities; subscriptions remain unproven.',
            'only_axiom and only_j7 count unmatched observations, not provider misses.',
            'Unobserved J7 events have no receipt; their downstream timestamps only select the report interval.',
            'Seven continuous days and complete subscription intervals are required before a source verdict.',
        ],
    }


def _address(chain, value):
    value = str(value or '').strip()
    return value if str(chain) == 'solana' else value.lower()


def _signature_column(conn):
    columns = {r[1] for r in conn.execute('PRAGMA table_info(memecoin_events)')}
    return next((name for name in SIGNATURE_COLUMNS if name in columns), None)


def _epoch(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _window_sql(column, since, until):
    clauses, args = [], []
    for bound, op in ((since, '>='), (until, '<')):
        if bound is not None:
            if _epoch(bound) is None:
                raise ValueError('Invalid receipt window timestamp')
            clauses.append(f'julianday({column}) {op} julianday(?)')
            args.append(str(bound))
    return clauses, args


def _record(row, *, uid, chain, mint, side, amount, signature):
    return {'id': row.get('id', row.get('fomo_event_id')), 'uid': str(uid or ''),
            'chain': str(chain or ''), 'mint': _address(chain, mint),
            'side': str(side or '').lower(), 'amount': amount,
            'sig': _address(chain, signature) if signature else None,
            'at': _epoch(row.get('received_at')), 'mono': row.get('received_mono_ns'),
            'clock_domain': row.get('clock_domain'), 'capture_run_id': row.get('capture_run_id')}


def _valid_identity(row):
    return bool(row['uid'] and row['chain'] and row['mint'] and row['side'] in ('buy', 'sell')
                and row['at'] is not None)


def _axiom_rows(conn, owners, chain, since, until):
    out, limits = defaultdict(list), Counter()
    limits['provenance'] = Counter()
    clauses, args = _window_sql('received_at', since, until)
    if chain:
        clauses.append('chain=?')
        args.append(str(chain))
    sql = 'SELECT * FROM axiom_wallet_events'
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    for record in conn.execute(sql + ' ORDER BY id', args):
        row = dict(record)
        if row.get('observation_kind') != 'live' or row.get('transport') != 'ws':
            limits['excluded'] += 1
            limits['provenance'][str(row.get('observation_kind') or 'unknown')] += 1
            continue
        owner = owners.get((str(row['chain']), _address(row['chain'], row['address'])))
        if owner is None:
            limits['unattributed'] += 1
            continue
        item = _record(row, uid=owner[0], chain=row['chain'], mint=row['mint'],
                       side=row['side'], amount=row.get('amount_usd'), signature=row.get('tx_sig'))
        if not _valid_identity(item):
            limits['unmatchable'] += 1
            continue
        out[item['chain']].append(item)
    return out, limits


def _j7_rows(conn, cohort, chain, since, until, signature_column):
    out, limits, unobserved = defaultdict(list), Counter(), Counter()
    clauses, args = _window_sql('r.received_at', since, until)
    # Raw receipts remain observable when a downstream gate discards the event.
    sql = ('SELECT r.*, e.source_user_id AS event_uid, e.network_id AS event_chain, '
           'e.mint AS event_mint, e.side AS event_side, e.their_size_usd AS event_amount')
    if signature_column:
        sql += f', e.{signature_column} AS event_sig'
    sql += ' FROM j7_receipts r LEFT JOIN memecoin_events e ON e.fomo_event_id=r.fomo_event_id'
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    for record in conn.execute(sql + ' ORDER BY r.received_at, r.fomo_event_id', args):
        row = dict(record)
        try:
            identity = json.loads(row.get('identity_json') or '{}')
        except (ValueError, TypeError):
            limits['unmatchable'] += 1
            continue
        if not isinstance(identity, dict):
            limits['unmatchable'] += 1
            continue
        item = _record(row, uid=identity.get('source_user_id', row['event_uid']),
                       chain=identity.get('chain', row['event_chain']),
                       mint=identity.get('mint', row['event_mint']),
                       side=identity.get('side', row['event_side']),
                       amount=identity.get('amount_usd', row['event_amount']),
                       signature=identity.get('tx_sig', row.get('event_sig')))
        if chain and item['chain'] != str(chain):
            continue
        if not _valid_identity(item):
            limits['unmatchable'] += 1
            continue
        if (item['chain'], item['uid']) not in cohort:
            limits['unattributed'] += 1
            continue
        out[item['chain']].append(item)
    clauses, args = _window_sql('e.received_at', since, until)
    sql = ('SELECT e.network_id, e.source_user_id FROM memecoin_events e '
           'LEFT JOIN j7_receipts r ON r.fomo_event_id=e.fomo_event_id '
           "WHERE r.fomo_event_id IS NULL AND e.signal_source='follow_activity'")
    if clauses:
        sql += ' AND ' + ' AND '.join(clauses)
    for c, uid in conn.execute(sql, args):
        if (not chain or str(c) == str(chain)) and (str(c), str(uid)) in cohort:
            unobserved[str(c)] += 1
    return out, limits, unobserved


def _amount_agreement(a, b):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return 'absent'
    if not math.isfinite(a) or not math.isfinite(b) or a <= 0 or b <= 0:
        return 'absent'
    return 'agree' if abs(a - b) <= AMOUNT_TOLERANCE * max(a, b) else 'disagree'


def _identity(row):
    return row['chain'], row['uid'], row['mint'], row['side']


def _deduplicate(rows):
    out, seen, duplicates = [], set(), 0
    for row in rows:
        key = (*_identity(row), row['sig'])
        if row['sig'] and key in seen:
            duplicates += 1
            continue
        if row['sig']:
            seen.add(key)
        out.append(row)
    return out, duplicates


def _compare_one(axiom_rows, j7_rows, window_s):
    arows, duplicates = _deduplicate(axiom_rows)
    jrows, j7_duplicates = _deduplicate(j7_rows)
    groups = defaultdict(list)
    for j, row in enumerate(jrows):
        groups[_identity(row)].append(j)
    used_a, used_j, ambiguous_a, ambiguous_j, pairs = set(), set(), set(), set(), []
    for exact in (True, False):
        edges, reverse = defaultdict(list), defaultdict(list)
        for a, row in enumerate(arows):
            if a in used_a or a in ambiguous_a:
                continue
            for j in groups[_identity(row)]:
                other = jrows[j]
                if j in used_j or j in ambiguous_j:
                    continue
                diff = _arrival_diff_ms(row, other)
                distance_s = abs(diff) / 1000 if diff is not None else abs(row['at'] - other['at'])
                if distance_s > window_s:
                    continue
                both = bool(row['sig'] and other['sig'])
                if both and row['sig'] != other['sig']:
                    continue
                if exact != both:
                    continue
                agreement = _amount_agreement(row['amount'], other['amount'])
                if not exact and agreement == 'disagree':
                    continue
                kind = 'exact' if exact else ('weak' if agreement == 'agree' else 'weak_no_amount')
                edges[a].append((j, kind))
                reverse[j].append(a)
        for a, candidates in edges.items():
            j, kind = candidates[0]
            if len(candidates) == 1 and len(reverse[j]) == 1:
                used_a.add(a)
                used_j.add(j)
                pairs.append((arows[a], jrows[j], kind))
            else:
                ambiguous_a.add(a)
                ambiguous_j.update(j for j, _ in candidates)
    counts, timings = Counter(), {kind: [] for kind in MATCH_KINDS}
    incomparable = 0
    for a, j, kind in pairs:
        counts[kind] += 1
        diff = _arrival_diff_ms(a, j)
        if diff is None:
            incomparable += 1
        else:
            timings[kind].append(diff)
    nonempty = [values for values in timings.values() if values]
    # A combined percentile would mix exact identity with uncertain identity.
    aggregate = nonempty[0] if len(nonempty) == 1 else []
    return {'matched': len(pairs), **{kind: counts[kind] for kind in MATCH_KINDS},
            'duplicates': duplicates, 'j7_duplicates': j7_duplicates,
            'ambiguous_axiom': len(ambiguous_a), 'ambiguous_j7': len(ambiguous_j),
            'only_axiom': len(arows) - len(used_a) - len(ambiguous_a),
            'only_j7': len(jrows) - len(used_j) - len(ambiguous_j),
            'timed': sum(map(len, timings.values())), 'clock_incomparable': incomparable,
            'median_diff_ms': _percentile(aggregate, 50), 'p95_diff_ms': _percentile(aggregate, 95),
            'latency_by_match': {kind: {'matched': counts[kind], 'timed': len(values),
                                      'median_diff_ms': _percentile(values, 50),
                                      'p95_diff_ms': _percentile(values, 95)}
                                 for kind, values in timings.items()},
            'observations': {'axiom': _observation_summary(axiom_rows),
                             'j7': _observation_summary(j7_rows)}}


def _arrival_diff_ms(axiom_row, j7_row):
    domain = axiom_row.get('clock_domain')
    if not domain or domain != j7_row.get('clock_domain'):
        return None
    try:
        return (int(axiom_row['mono']) - int(j7_row['mono'])) / 1_000_000.0
    except (TypeError, ValueError, OverflowError):
        return None


def _observation_summary(rows):
    times = [row['at'] for row in rows if row['at'] is not None]
    return {'rows': len(rows),
            'first_received_at': datetime.fromtimestamp(min(times), timezone.utc).isoformat() if times else None,
            'last_received_at': datetime.fromtimestamp(max(times), timezone.utc).isoformat() if times else None,
            'observed_span_days': (max(times) - min(times)) / 86400 if times else 0,
            'clock_domains': len({r['clock_domain'] for r in rows if r['clock_domain']}),
            'unknown_clock_rows': sum(not r['clock_domain'] for r in rows),
            'capture_runs': len({r['capture_run_id'] for r in rows if r['capture_run_id']})}


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]

"""Bounded, admission-independent archival of the active coin swap producer.

This adapter has no network seam. Producer frontiers describe subscribed swaps,
not complete pricing state. Immutable snapshots retain revisions; the source has
no revision journal, so neither a quiet second nor an executable path is inferred.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time

from sniper.persistence.db_core import get_readonly_connection
from sniper.persistence import db_wallet_execution as store
from sniper.research.wallet_execution.evidence import digest

PRECONTEXT_S = 120
MINIMUM_TAIL_S = 1800
MISSING = ['complete_venue_state_missing', 'quantity_bound_pricing_missing',
           'source_paid_swap_proof_missing', 'prior_cost_latency_calibration_missing',
           'source_revision_journal_missing']


def _epoch(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('capture requires an aware clock')
    return dt.timestamp()


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds')


def _state(db, key):
    cp = store.checkpoint(db, 'ordered_coin:' + key)
    return json.loads(cp['epoch']) if cp else None


def _save_state(db, key, state):
    # Generic append-only checkpoints carry research queue state only.
    store.record_checkpoint(db, 'ordered_coin:' + key, state.get('cycle', 0),
                            state.get('source_identity'), None, store.encoded(state))


def _snapshot(db, source, identity, coin, row):
    body = {'source_identity': identity, 'coin': coin, 'row': row}
    before = db.total_changes
    store.record_raw(db, source, digest(body), body)
    return db.total_changes - before


def _gap(db, coin, now, reason):
    source = 'ordered_coin:' + coin
    if not db.execute('SELECT 1 FROM we_source_gaps WHERE source=? AND reason=? LIMIT 1',
                      (source, reason)).fetchone():
        db.execute('INSERT INTO we_source_gaps(source,at,reason) VALUES (?,?,?)',
                   (source, now, reason))


def _enroll(db, now, limit):
    offset = store.cursor(db, 'ordered_origins') or 0
    studies=[(r['study_id'],json.loads(r['payload'])) for r in db.execute('SELECT study_id,payload FROM we_studies')]
    origins = db.execute('SELECT rowid AS intake_rowid,* FROM we_origins WHERE rowid>? '
                         'ORDER BY rowid LIMIT ?', (offset, limit)).fetchall()
    with db:
        for row in origins:
            source = json.loads(row['payload'])
            mint = row['mint']
            if row['chain'] != 'solana' and isinstance(mint, str):
                mint = mint.lower()
            coin = {'chain': row['chain'], 'mint': mint}
            key = digest(coin)
            try:
                receipt = _epoch(row['observed_at'])
                matches=[(key,plan) for key,plan in studies if plan['wallet']==row['wallet']
                         and str(plan['chain'])==row['chain']
                         and plan['validation_start']<=receipt<=plan['test_end']]
                tail=max([MINIMUM_TAIL_S, *(max(plan.get('outcome_tail_s',0),
                          max(p.get('max_wait_s',0)+p['max_hold_s'] for p in [plan['baseline'],*plan['challengers']]))
                          for _,plan in matches)])
                start, end = receipt - PRECONTEXT_S, receipt + tail
                clock_status = 'receipt_clock_known'
            except (ValueError, TypeError, OverflowError):
                start = end = None
                matches=[]
                clock_status = 'receipt_clock_unknown'
            request = {**coin, 'coin_key': key, 'origin_id': row['origin_id'],
                       'wallet': row['wallet'], 'source_digest': row['raw_digest'],
                       'source_ts': source.get('source_ts'), 'received_at': row['observed_at'],
                       'window_start': start, 'window_end': end,
                       'study_ids':[key for key,_ in matches],
                       'clock_status': clock_status, 'denominator': 'committed_feed_buy'}
            store.record_raw(db, 'ordered_requests', row['origin_id'], request)
            store.record_raw(db, 'ordered_coins', key, coin)
            state = _state(db, key)
            if start is not None:
                if state is None or start > state['window_end']:
                    if state is not None and state['status'] != 'archived_swap_window_snapshot':
                        cut = {'coin': key, 'at': now, 'previous_window': state,
                               'reason': 'prior_window_unfinished'}
                        store.record_raw(db, 'ordered_window_cuts', digest(cut), cut)
                        _gap(db, key, now, 'prior_window_unfinished')
                    state = {**coin, 'window_start': start, 'window_end': end,
                             'cursor': None, 'cycle': 0, 'status': 'queued'}
                else:
                    if start < state['window_start']:
                        state['cursor'] = None
                    state.update(window_start=min(start, state['window_start']),
                                 window_end=max(end, state['window_end']), status='queued')
                _save_state(db, key, state)
            else:
                _gap(db, key, now, clock_status)
            offset = row['intake_rowid']
        store.advance_cursor(db, 'ordered_origins', offset)
    backlog = db.execute('SELECT count(*) FROM we_origins WHERE rowid>?', (offset,)).fetchone()[0]
    return len(origins), backlog


def _coin_page(db, limit):
    cp = store.checkpoint(db, 'ordered_coin_queue')
    anchor = cp['anchor'] if cp else ''
    rows = db.execute('SELECT raw_id,payload FROM we_raw WHERE source=? AND raw_id>? '
                      'ORDER BY raw_id LIMIT ?', ('ordered_coins', anchor or '', limit)).fetchall()
    if not rows and anchor:
        rows = db.execute('SELECT raw_id,payload FROM we_raw WHERE source=? '
                          'ORDER BY raw_id LIMIT ?', ('ordered_coins', limit)).fetchall()
    return rows


def _read_coin(source, state, *, row_limit, pool_limit=16):
    """One short read-only snapshot; keyset pages cycle to observe late revisions."""
    chain, mint = state['chain'], state['mint']
    source.execute('BEGIN')
    try:
        feed_row = source.execute('SELECT * FROM volume_feed WHERE chain_id=?', (chain,)).fetchone()
        feed = dict(feed_row) if feed_row else {'chain_id': chain, 'since': None,
                                               'through': None, 'received': None}
        feed['scope'] = 'producer_reported_swap_frontier'
        mappings = [dict(r) for r in source.execute(
            'SELECT * FROM onchain_pool_protocols WHERE chain_id=? AND mint=? LIMIT ?',
            (chain, mint, pool_limit + 1))]
        gaps = []
        if len(mappings) > pool_limit:
            return {'feed': feed, 'gaps': ['pool_budget_skipped'], 'rows': [], 'headers': [],
                    'metadata': [], 'tick_gaps': [], 'resets': [], 'mappings': [], 'more': False}
        pools = sorted({r['pool'] for r in mappings if r.get('pool')})
        metadata = []
        for pool in pools:
            row = source.execute('SELECT * FROM pool_meta WHERE chain_id=? AND pool=?',
                                 (chain, pool)).fetchone()
            if row:
                metadata.append(dict(row))
        lower = source.execute('SELECT * FROM block_times WHERE chain_id=? AND block_ts<=? '
                               'ORDER BY block_no DESC LIMIT 1',
                               (chain, _iso(state['window_start']))).fetchone()
        if lower is None:
            lower = source.execute('SELECT * FROM block_times WHERE chain_id=? '
                                   'ORDER BY block_no LIMIT 1', (chain,)).fetchone()
            gaps.append('precontext_retained_short')
        upper = source.execute('SELECT * FROM block_times WHERE chain_id=? AND block_ts<=? '
                               'ORDER BY block_no DESC LIMIT 1',
                               (chain, _iso(state['scan_end']))).fetchone()
        if lower is None or upper is None:
            return {'feed': feed, 'gaps': gaps + ['header_window_missing'], 'rows': [],
                    'headers': [], 'metadata': metadata, 'tick_gaps': [], 'resets': [],
                    'mappings': mappings, 'more': False}
        lo, hi = lower['block_no'], upper['block_no']
        if not pools:
            gaps.append('coin_pool_identity_missing')
        rows = []
        if pools:
            cursor = state.get('cursor') or [lo - 1, -1, -1]
            marks = ','.join('?' for _ in pools)
            rows = [dict(r) for r in source.execute(
                'SELECT * FROM pool_ticks WHERE chain_id=? AND pool IN (' + marks + ') '
                'AND block_no>=? AND block_no<=? '
                'AND (block_no,COALESCE(log_index,-1),id)>(?,?,?) '
                'ORDER BY block_no,COALESCE(log_index,-1),id LIMIT ?',
                (chain, *pools, lo, hi, *cursor, row_limit + 1))]
        more = len(rows) > row_limit
        rows = rows[:row_limit]
        blocks = sorted({lo, hi, *(r['block_no'] for r in rows)})
        headers = [dict(r) for r in source.execute(
            'SELECT * FROM block_times WHERE chain_id=? AND block_no IN (' +
            ','.join('?' for _ in blocks) + ') ORDER BY block_no', (chain, *blocks))]
        tick_gaps = [dict(r) for r in source.execute(
            'SELECT * FROM tick_gaps WHERE chain_id=? AND from_block<=? AND to_block>=? '
            'ORDER BY id LIMIT ?', (chain, hi, lo, row_limit + 1))]
        resets = [dict(r) for r in source.execute(
            'SELECT rowid AS source_rowid,* FROM volume_feed_resets WHERE chain_id=? '
            'AND at>=? AND at<=? ORDER BY at,rowid LIMIT ?',
            (chain, state['window_start'], state['scan_end'], row_limit + 1))]
        if len(tick_gaps) > row_limit or len(resets) > row_limit:
            gaps.append('revision_budget_skipped')
        return {'feed': feed, 'gaps': gaps, 'rows': rows, 'headers': headers,
                'metadata': metadata, 'tick_gaps': tick_gaps[:row_limit],
                'resets': resets[:row_limit], 'mappings': mappings, 'more': more}
    finally:
        source.rollback()


def archive_ordered_once(db, source_path, *, now, origin_limit=500, coin_limit=8,
                         row_limit=500, deadline_s=1.0):
    """Persist real inputs for future pricing; never construct an eligible episode.

    Limits bound origins, coins, tick rows and SQLite work per pass. Fair keyset
    rotation prevents a busy coin monopolizing the archive. Unknown source state,
    retention and deadline cuts are explicit diagnostics, not quiet coverage.
    """
    origin_limit = max(1, min(int(origin_limit), 2000))
    coin_limit = max(1, min(int(coin_limit), 32))
    row_limit = max(1, min(int(row_limit), 2000))
    report = {'source_status': 'ok', 'provider_requests': 0,
              'execution_path_status': 'blocked', 'missing': MISSING,
              'precontext_s': PRECONTEXT_S, 'minimum_tail_s': MINIMUM_TAIL_S,
              'source_kind': 'active_coin_swap_producer', 'row_limit': row_limit,
              'coin_limit': coin_limit, 'archived_ticks': 0, 'scanned_ticks': 0,
              'processed_coins': 0, 'tail_pending_coins': 0, 'header_mismatches': 0,
              'tail_pending_scope': 'processed_coin_page', 'skipped_expired_coins': 0,
              'observed_frontiers': [], 'collection_gaps': [], 'coins': []}
    started = time.monotonic()
    report['enrolled_origins'], report['origin_backlog'] = _enroll(db, now, origin_limit)
    try:
        at = _epoch(now)
        path = Path(source_path)
        stat = path.stat()
        identity = f'{stat.st_dev}:{stat.st_ino}'
        report['source_identity'] = identity
        deadline = time.monotonic() + max(.05, min(float(deadline_s), 2.0))
        with closing(get_readonly_connection(str(path))) as source:
            source.execute('PRAGMA busy_timeout=50')
            source.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            for item in _coin_page(db, coin_limit):
                if report['scanned_ticks'] >= row_limit or time.monotonic() >= deadline:
                    report['collection_gaps'].append('capture_budget_skipped')
                    break
                key, coin = item['raw_id'], json.loads(item['payload'])
                state = _state(db, key)
                if state is None:
                    report['collection_gaps'].append('receipt_clock_unknown')
                    with db:
                        store.record_checkpoint(db, 'ordered_coin_queue', 0, identity, key, '')
                    continue
                if (state['status'] == 'archived_swap_window_snapshot' and
                        at > state['window_end'] + 60):
                    report['skipped_expired_coins'] += 1
                    with db:
                        store.record_checkpoint(db, 'ordered_coin_queue', 0, identity, key, '')
                    continue
                previous = state.get('source_identity')
                if previous is not None and previous != identity:
                    state['cursor'] = None
                    report['collection_gaps'].append('source_replaced')
                state['source_identity'] = identity
                state['scan_end'] = min(at, state['window_end'])
                data = _read_coin(source, state, row_limit=row_limit-report['scanned_ticks'])
                feed = data['feed']
                report['observed_frontiers'].append(feed)
                report['collection_gaps'].extend(data['gaps'])
                if previous is not None and previous != identity:
                    data['gaps'].append('source_replaced')
                if any(not isinstance(r.get('log_index'), int) or r['log_index'] < 0
                       for r in data['rows']):
                    data['gaps'].append('ordering_fields_missing')
                    report['collection_gaps'].append('ordering_fields_missing')
                header_map = {r['block_no']: r for r in data['headers']}
                mismatches = sum(1 for r in data['rows'] if r.get('block_hash') is None or
                                 r['block_no'] not in header_map or
                                 r['block_hash'] != header_map[r['block_no']].get('block_hash'))
                if mismatches:
                    data['gaps'].append('header_identity_unknown_or_changed')
                    report['collection_gaps'].append('header_identity_unknown_or_changed')
                with db:
                    for row in data['rows']:
                        report['archived_ticks'] += _snapshot(db, 'ordered_ticks', identity, key, row)
                    for name, field in [('ordered_headers','headers'), ('ordered_metadata','metadata'),
                                        ('ordered_gaps','tick_gaps'), ('ordered_resets','resets'),
                                        ('ordered_pool_mappings','mappings')]:
                        for row in data[field]:
                            _snapshot(db, name, identity, key, row)
                    _snapshot(db, 'ordered_frontiers', identity, key, feed)
                    orders = [[r['block_no'], r.get('log_index'), r['id']] for r in data['rows']]
                    page = {'coin': key, 'source_identity': identity, 'captured_at': now,
                            'window_start': state['window_start'], 'window_end': state['window_end'],
                            'orders': orders, 'tick_digests': [digest(r) for r in data['rows']],
                            'cycle': state['cycle'], 'more': data['more'], 'gaps': data['gaps'],
                            'source_frontier': digest(feed), 'revision_completeness': 'unknown'}
                    store.record_raw(db, 'ordered_pages', digest(page), page)
                    for reason in data['gaps']:
                        _gap(db, key, now, reason)
                    state['cursor'] = ([orders[-1][0], orders[-1][1] if orders[-1][1] is not None else -1,
                                        orders[-1][2]] if data['more'] and orders else None)
                    if not data['more']:
                        state['cycle'] += 1
                    through = feed.get('through')
                    tail_pending = (not isinstance(through, (int,float)) or
                                    not math.isfinite(through) or through < state['window_end'] or
                                    data['more'])
                    state['status'] = 'capturing' if tail_pending else 'archived_swap_window_snapshot'
                    _save_state(db, key, state)
                    store.record_checkpoint(db, 'ordered_coin_queue', 0, identity, key, '')
                report['scanned_ticks'] += len(data['rows'])
                report['processed_coins'] += 1
                report['tail_pending_coins'] += int(tail_pending)
                report['header_mismatches'] += mismatches
                report['coins'].append({**coin, 'window_start': state['window_start'],
                                        'window_end': state['window_end'], 'status': state['status']})
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        db.rollback()
        report.update(source_status='unavailable', reason=type(exc).__name__)
        report['collection_gaps'].append('source_read_failed_or_budget_exhausted')
    report['collection_gaps'] = sorted(set(report['collection_gaps']))
    report['elapsed_ms'] = (time.monotonic() - started) * 1000
    return report

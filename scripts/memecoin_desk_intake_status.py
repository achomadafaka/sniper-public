#!/usr/bin/env python3
"""Read-only intake telemetry: text or --json, no network, migrations, or runtime actions.

Poll/item windows use host receive seconds; first sightings use corrected UTC. Shares describe
stored item observations (repeated appearances count), not unique coins or trading performance.
Local mapping coverage is current evidence, not graduation prevalence at first sighting.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services.memecoin_desk_intake import DEXSCREENER_FEEDS, PUMPFUN_LISTS  # noqa: E402
from sniper.feeds.fomo_intake import LIST_LABELS  # noqa: E402

FILTERS = {'pumpfun': tuple(PUMPFUN_LISTS), 'dexscreener': tuple(DEXSCREENER_FEEDS.values()),
           'fomo': tuple(LIST_LABELS)}
WINDOWS = {'1h': 3600, '24h': 86400}
WSOL = 'So11111111111111111111111111111111111111112'


def read(path, sql, args=()):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def _offset(root):
    try:
        value = json.loads((root / 'data/clock_offset.json').read_text())['offset_s']
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _files(folder):
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix == '.db') if folder.is_dir() else []


def _mapping(root, now):
    mapped, errors, sources = set(), [], []
    for path in _files(root / 'data/desk/pumpswap'):
        try:
            tables = {r['name'] for r in read(path, "SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('pools', 'ps_grads'):
                if table not in tables:
                    continue
                canonical = ' AND canonical=1' if table == 'pools' else ''
                rows = read(path, 'SELECT DISTINCT mint FROM ' + table +
                            ' WHERE quote_mint=? AND received_at<?' + canonical, (WSOL, now))
                mapped.update(r['mint'] for r in rows)
                sources.append(path.name + ':' + table)
        except (OSError, sqlite3.Error) as exc:
            errors.append(path.name + ':' + type(exc).__name__)
    return {'source': 'unknown' if errors else 'available' if sources else 'not running',
            'errors': errors, 'tables': sources}, mapped


def _empty():
    return dict(polls=None, newest_error_class=None, items=None, new_first_sightings=None,
                chains=None, solana_share=None, provider_graduated=None,
                provider_graduated_share=None, locally_mapped=None, locally_mapped_share=None)


def _metric(polls, items, first, mapped, mapping_ok):
    outcomes = Counter()
    for row in polls:
        error = str(row['error'] or '')
        refused = (row['status'] == 'refused' or error.startswith('refused') or
                   error in {'challenge', 'login_expired', 'login_required', 'auth_unproven', 'http_403', 'http_429'})
        key = 'refused' if refused else 'error' if row['status'] == 'error' else 'ok' if row['status'] == 'ok' else 'other'
        outcomes[key] += 1
    errors = [r for r in polls if r['error']]
    newest = max(errors, key=lambda r: r['received_at'])['error'] if errors else None
    chains, venues = Counter(), Counter()
    for row in items:
        chains[row['network_id'] or 'unknown'] += row['n']
        if row['network_id'] == 'solana':
            venues['yes' if row['venue'] == 'pumpswap' else 'no' if row['venue'] == 'pump_curve' else 'unknown'] += row['n']
    count, sol = sum(chains.values()), chains['solana']
    known_venues = venues['yes'] + venues['no']
    local = sum(r['n'] for r in items if r['network_id'] == 'solana' and r['mint'] in mapped) if mapping_ok else None
    known_items = [r['items'] for r in polls if r['items'] is not None]
    # A failed poll does not reveal an empty list. Mixed successes retain only their observed item count.
    nitems = sum(known_items) if known_items else None if polls else 0
    return dict(polls={k: outcomes[k] for k in ('ok', 'error', 'refused', 'other')},
                newest_error_class=newest, items=nitems, new_first_sightings=first,
                chains=dict(chains), solana_share=sol / count if count else None,
                provider_graduated={k: venues[k] for k in ('yes', 'no', 'unknown')},
                provider_graduated_share=venues['yes'] / known_venues if known_venues else None,
                locally_mapped=local, locally_mapped_share=local / sol if local is not None and sol else None)


def _feed(root, feed, now, offset, mapped, mapping_ok):
    folder = root / 'data/desk/intake' / feed
    paths = _files(folder)
    daily = []
    for path in paths:
        try:
            day = datetime.strptime(path.stem, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        if day <= now and day + 86400 > now - 86400:
            daily.append(path)
    out = {'source': 'available' if daily else 'not running', 'errors': [],
           'day_files': [p.name for p in daily], 'windows': {}}
    first_path = folder / 'first_sightings.db'
    for window, seconds in WINDOWS.items():
        polls, items, firsts = [], [], None
        try:
            for path in daily:
                polls.extend(read(path, 'SELECT filter,received_at,status,items,error FROM polls '
                                  'WHERE feed=? AND received_at>=? AND received_at<?', (feed, now - seconds, now)))
                items.extend(read(path, 'SELECT filter,network_id,mint,venue,COUNT(*) AS n FROM intake_rows '
                                  'WHERE feed=? AND host_received_at>=? AND host_received_at<? '
                                  'GROUP BY filter,network_id,mint,venue', (feed, now - seconds, now)))
        except (OSError, sqlite3.Error) as exc:
            out['source'] = 'unknown'
            out['errors'].append(type(exc).__name__)
        if first_path.is_file() and offset is not None:
            try:
                firsts = read(first_path, 'SELECT filter,COUNT(*) AS n FROM first_sightings '
                              'WHERE feed=? AND observed_at>=? AND observed_at<? GROUP BY filter',
                              (feed, now - offset - seconds, now - offset))
            except (OSError, sqlite3.Error) as exc:
                out['errors'].append('first_sightings:' + type(exc).__name__)
        labels = sorted(set(FILTERS.get(feed, ())) | {r['filter'] for r in polls + items})
        metrics = {}
        for filt in ['*'] + labels:
            selected_polls = [r for r in polls if filt == '*' or r['filter'] == filt]
            selected_items = [r for r in items if filt == '*' or r['filter'] == filt]
            first = sum(r['n'] for r in firsts if filt == '*' or r['filter'] == filt) if firsts is not None else None
            metrics[filt] = (_metric(selected_polls, selected_items, first, mapped, mapping_ok)
                             if out['source'] == 'available' else _empty())
        out['windows'][window] = {'filters': metrics}
    if out['source'] == 'unknown':
        for value in out['windows'].values():
            value['filters'] = {k: _empty() for k in value['filters']}
    out['errors'] = sorted(set(out['errors']))
    return out


def _emitter(root, now):
    desk = root / 'data/desk'
    path = desk / 'intake_emitter.db'
    source, outcomes, errors = 'not running', [], []
    if path.is_file():
        try:
            outcomes = read(path, 'SELECT feed,filter,reason,at_host FROM outcomes WHERE at_host>=? AND at_host<?',
                            (now - 86400, now))
            source = 'available'
        except (OSError, sqlite3.Error) as exc:
            source = 'unknown'
            errors.append('outcomes:' + type(exc).__name__)
    emissions, mapped = [], set()
    folder = desk / 'candidate_emissions_intake'
    emission_source = 'not running'
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            try:
                day = datetime.strptime(path.stem, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if path.suffix != '.jsonl' or not (day <= now and day + 86400 > now - 86400):
                continue
            if emission_source == 'not running':
                emission_source = 'available'
            try:
                with path.open() as stream:
                    for line in stream:
                        c = json.loads(line)
                        at = datetime.fromisoformat(c['emitted_at'].replace('Z', '+00:00'))
                        if at.tzinfo is None:
                            raise ValueError('naive emission time')
                        intake = c['intake']
                        row = dict(feed=intake['feed'], filter=intake['filter'], ticket=c['ticket_id'],
                                   at_host=at.timestamp())
                        if not all(isinstance(row[k], str) and row[k] for k in ('feed', 'filter', 'ticket')):
                            raise ValueError('emission identity missing')
                        if now - 86400 <= row['at_host'] < now:
                            emissions.append(row)
                            state = c.get('snapshot', {}).get('fields', {}).get('pool_state', {})
                            if (c.get('network_id') == 'solana' and c.get('schema') == 'desk-candidate/pumpswap-v1'
                                    and state.get('value', {}).get('map_source') == 'intake_canonical_pda'
                                    and isinstance(c.get('mint'), str)):
                                mapped.add(c['mint'])
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                emission_source = 'unknown'
                errors.append(path.name + ':' + type(exc).__name__)
    windows = {}
    for window, seconds in WINDOWS.items():
        if source == 'not running' and emission_source == 'not running':
            windows[window] = None
            continue
        rows = [r for r in outcomes if r['at_host'] >= now - seconds]
        appends = [r for r in emissions if r['at_host'] >= now - seconds]
        groups = {('*', '*')} | {(r['feed'], r['filter']) for r in rows + appends}
        groups |= {(feed, '*') for feed, _ in groups if feed != '*'}
        filters = {}
        for feed, filt in sorted(groups):
            selected = [r for r in rows if (feed == '*' or r['feed'] == feed) and (filt == '*' or r['filter'] == filt)]
            logs = [r for r in appends if (feed == '*' or r['feed'] == feed) and (filt == '*' or r['filter'] == filt)]
            counts = Counter(r['reason'] for r in selected)
            filters.setdefault(feed, {})[filt] = {
                'outcomes': {k: counts[k] for k in ('claimed', 'emitted', 'cooldown', 'no_print')}
                    if source == 'available' else None,
                'other_outcomes': sum(v for k, v in counts.items() if k not in {'claimed', 'emitted', 'cooldown', 'no_print'})
                    if source == 'available' else None,
                'emission_rows': len(logs) if emission_source == 'available' else None,
                'emission_tickets': len({r['ticket'] for r in logs}) if emission_source == 'available' else None}
        windows[window] = {'filters': filters}
    return {'source': source, 'emission_source': emission_source, 'errors': errors, 'windows': windows}, mapped


def build_report(root, *, now=None):
    root = Path(root)
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError('now must be finite host seconds')
    offset = _offset(root)
    mapping, mapped = _mapping(root, now)
    emitter, derived = _emitter(root, now)
    if derived and emitter['emission_source'] == 'available':
        mapped.update(derived)
        if mapping['source'] == 'not running':
            mapping['source'] = 'available'
        mapping['tables'].append('candidate_emissions_intake:intake_canonical_pda')
    return {'as_of_host': now, 'clock_offset_s': offset,
            'window_clocks': {'polls_items': 'host_received_at', 'first_sightings': 'corrected_UTC'},
            'share_denominators': {'solana': 'all stored item observations, including unknown chains',
                                   'provider_graduated': 'Solana observations with known venue',
                                   'locally_mapped': 'Solana observations; mapping known at report time'},
            'mapping': mapping,
            'feeds': {feed: _feed(root, feed, now, offset, mapped, mapping['source'] == 'available') for feed in FILTERS},
            'emitter': emitter}


def text_report(report):
    lines = ['Intake status (polls/items: host receive clock; first sightings: corrected UTC)',
             'Shares count stored observations, not unique coins. unknown = missing evidence.']
    for feed, source in report['feeds'].items():
        lines.append(feed + ': ' + source['source'])
        for window, data in source['windows'].items():
            for filt, row in data['filters'].items():
                values = ' '.join(k + '=' + ('unknown' if v is None else json.dumps(v, sort_keys=True)) for k, v in row.items())
                lines.append('  ' + window + ' ' + filt + ' ' + values)
        if source['errors']:
            lines.append('  source_errors=' + ','.join(source['errors']))
    lines.append('emitter outcomes: ' + report['emitter']['source'] + '; append log: ' + report['emitter']['emission_source'])
    for window, data in report['emitter']['windows'].items():
        lines.append('  ' + window + ' ' + ('unknown' if data is None else json.dumps(data, sort_keys=True)))
    if report['emitter']['errors']:
        lines.append('  source_errors=' + ','.join(report['emitter']['errors']))
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', type=Path, default=Path(os.environ.get('SNIPER_STATE_ROOT') or ROOT))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    report = build_report(args.state_root)
    print(json.dumps(report, sort_keys=True, allow_nan=False) if args.json else text_report(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

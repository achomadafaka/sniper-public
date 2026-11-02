#!/usr/bin/env python3
"""Bounded observation on home/FRA hosts using the existing research feed protocol.

No DB reads, enrichment, signing, cloud provisioning or service changes. Use the
same frozen JSON list of wallet addresses on both hosts. Credentials come from
the environment or explicit --env-file paths; the Helius key must be isolated
from funded/watcher keys. The paid Shyft token may be shared deliberately.

    python3 scripts/research/solana_feed_host_benchmark.py run --host home \
        --owners-file owners.json --env-file /secure/feed.env --seconds 120 --out capture-home
    python3 scripts/research/solana_feed_host_benchmark.py compare capture-home capture-fra

Run both hosts at the same time. A calibration JSON is optional for recording,
but required on BOTH hosts to compare absolute arrivals. It must contain source,
utc_correction_ms (add to host UTC), uncertainty_ms, frequency_error_ppm,
measured_at_epoch_ns and valid_until_epoch_ns, established by independent clock
calibration. Without it, cross-host latency stays unknown. Provider created_at,
TCP RTT and Solana blockTime are never delivery-latency anchors.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import shred_shadow_test as protocol


def clock_anchor():
    before = time.monotonic_ns()
    epoch = time.time_ns()
    after = time.monotonic_ns()
    return {'mono_ns': (before + after) // 2, 'epoch_ns': epoch,
            'bracket_ns': (after - before + 1) // 2}


def load_owners(path):
    from solders.pubkey import Pubkey
    if Path(path).stat().st_size > 32768:
        raise ValueError('owners input too large')
    owners = json.loads(Path(path).read_text())
    if not isinstance(owners, list) or not 1 <= len(owners) <= 128:
        raise ValueError('owners must be a nonempty list of at most 128 addresses')
    try:
        for owner in owners:
            Pubkey.from_string(owner)
    except (TypeError, ValueError):
        raise ValueError('owners contain an invalid address') from None
    return sorted(set(owners))


def run(*, seconds, out, owners_file, host, env_files=(), calibration_file=None,
        max_messages=10000):
    if (not math.isfinite(seconds) or not 0 < seconds <= 900
            or type(max_messages) is not int or not 1 <= max_messages <= 100000
            or not isinstance(host, str) or not 1 <= len(host) <= 64):
        raise ValueError('invalid benchmark bounds')
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError('capture directory already contains data')
    owners = load_owners(owners_file)
    env = dict(os.environ)
    if env_files:
        from dotenv import dotenv_values
        for path in env_files:
            env.update({key: value for key, value in dotenv_values(path).items() if value is not None})
    import slot_lane_send_monitor as monitor
    from sniper.services.memecoin_shyft_signal import live_config
    from sniper.services.memecoin_slot_signal import ws_url_from_http
    ws_url = ws_url_from_http(monitor.pick_research_url(env))
    if not ws_url:
        raise ValueError('isolated research WebSocket URL missing')
    token, urls = live_config({'MEMECOIN_SHYFT_LIVE_ENABLED': '1',
        'MEMECOIN_SHYFT_GRPC_TOKEN': env.get('MEMECOIN_SHYFT_GRPC_TOKEN')
                                    or env.get('MEMECOIN_RESEARCH_SHYFT_TOKEN', ''),
        **({'MEMECOIN_SHYFT_GRPC_URLS': env['MEMECOIN_SHYFT_GRPC_URLS']}
           if env.get('MEMECOIN_SHYFT_GRPC_URLS') else {})})
    calibration = (json.loads(Path(calibration_file).read_text()) if calibration_file else None)
    protocol._yellowstone_modules()  # existing research proto; compile before threads
    out.mkdir(parents=True, exist_ok=True)
    stop, head = threading.Event(), protocol.Head()
    rec = protocol.Recorder(out / 'events.jsonl', stop=stop, max_messages=max_messages,
        secrets=(token, parse_qs(urlsplit(ws_url).query).get('api-key', [''])[0]))
    start = clock_anchor()
    names = [('shyft:' + urlsplit(url).hostname, url) for url in urls]
    metadata = {'format': 'solana-feed-host-v1', 'host': host, 'owners': owners,
        'feeds': ['A', 'A2'] + [name for name, _url in names],
        'commitment': 'processed', 'scope': 'filtered_transaction_receipts; trade outcome unknown',
        'funded_enabled': False, 'seconds_bound': seconds, 'max_messages': max_messages,
        'clock_start': start, 'calibration': calibration}
    (out / 'capture.json').write_text(json.dumps(metadata, indent=2))
    threads = [threading.Thread(target=protocol.shred_feed,
        args=(url, token, owners, rec, stop, name, head), daemon=True) for name, url in names]
    deadline = time.monotonic() + seconds

    async def observe():
        tasks = [asyncio.create_task(protocol.helius_feed(name, ws_url, owners, rec, stop, head))
                 for name in ('A', 'A2')]
        try:
            for thread in threads:
                thread.start()
            while not stop.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.05)
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(observe())
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        rec.flush_bw()
        rec.write({'k': 'end', 't_ns': time.monotonic_ns(),
                   'why': rec.stop_reason or 'duration_or_interrupt',
                   'messages': rec.total_messages, 'duplicates': rec.duplicates})
        rec.close()
        metadata.update(clock_end=clock_anchor(), threads_still_alive=sum(t.is_alive() for t in threads))
        (out / 'capture.json').write_text(json.dumps(metadata, indent=2))
    return metadata


def calibrated_clock(meta):
    if meta.get('threads_still_alive'):
        return None, 'observer_shutdown_incomplete'
    calibration = meta.get('calibration')
    if calibration is None:
        return None, 'calibration_missing'
    try:
        correction, uncertainty, frequency = [float(calibration[key]) for key in
            ('utc_correction_ms', 'uncertainty_ms', 'frequency_error_ppm')]
        measured, valid = [int(calibration[key]) for key in
                               ('measured_at_epoch_ns', 'valid_until_epoch_ns')]
        start, end = meta['clock_start'], meta['clock_end']
        if (not calibration.get('source') or not all(math.isfinite(x) for x in
                (correction, uncertainty, frequency)) or uncertainty < 0 or frequency < 0
                or end['mono_ns'] <= start['mono_ns'] or end['epoch_ns'] <= start['epoch_ns']
                or start['bracket_ns'] < 0 or end['bracket_ns'] < 0):
            raise ValueError
        if measured > start['epoch_ns'] or valid < end['epoch_ns']:
            return None, 'calibration_expired'
        span_ns = end['mono_ns'] - start['mono_ns']
        shift_ms = abs(end['epoch_ns'] - start['epoch_ns'] - span_ns) / 1e6
        bracket_ms = (start['bracket_ns'] + end['bracket_ns']) / 1e6
        drift_ms = (end['epoch_ns'] - measured) / 1e9 * frequency / 1000
        if shift_ms > 2 * uncertainty + drift_ms + bracket_ms:
            return None, 'clock_step'
        bound = uncertainty + drift_ms + bracket_ms + shift_ms
        def convert(t_ns):
            return (start['epoch_ns'] + t_ns - start['mono_ns']) / 1e6 + correction
        return (convert, bound), None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, 'calibration_invalid'


def eligible_receipts(meta, events):
    windows = {name: protocol.up_windows(events, name) for name in
               set(row.get('feed') for row in events if row.get('feed'))}
    first = {}
    start = meta['clock_start']['mono_ns']
    end = meta.get('clock_end', {}).get('mono_ns', start)
    for row in events:
        if (row.get('k') == 'tx' and row.get('latency_eligible') is True
                and row.get('sig') and isinstance(row.get('slot'), int)
                and start <= row['t_ns'] <= end
                and protocol._in_one_window(windows.get(row['feed'], []), row)):
            key = (row['feed'], row['sig'])
            if key not in first or row['t_ns'] < first[key]['t_ns']:
                first[key] = row
    return first


def compare_captures(left, left_events, right, right_events):
    if any(meta.get('format') != 'solana-feed-host-v1'
           or meta.get('commitment') != 'processed' for meta in (left, right)):
        raise ValueError('unsupported capture protocol or commitment')
    if sorted(left['owners']) != sorted(right['owners']):
        raise ValueError('frozen owner universes differ')
    clocks = [calibrated_clock(meta) for meta in (left, right)]
    unknown = next((reason for _clock, reason in clocks if reason), None)
    lrows, rrows = eligible_receipts(left, left_events), eligible_receipts(right, right_events)
    out = {'left_host': left['host'], 'right_host': right['host'],
        'clock_status': {'left': clocks[0][1] or 'bounded', 'right': clocks[1][1] or 'bounded'},
        'delta': 'right_host_minus_left_host_ms; positive means left earlier',
        'scope': 'same-feed same-signature processed transaction receipts; no fill verdict',
        'unpaired': 'unpaired observations; feed loss is unproved', 'feeds': {}}
    feeds = set(left.get('feeds', [])) | set(right.get('feeds', [])) | {
        row['feed'] for row in left_events + right_events if row.get('feed')}
    for feed in sorted(feeds):
        lk, rk = {key for key in lrows if key[0] == feed}, {key for key in rrows if key[0] == feed}
        paired = [key for key in lk & rk if lrows[key]['slot'] == rrows[key]['slot']]
        deltas = []
        bound = None
        if unknown is None:
            (lc, lb), (rc, rb) = [clock for clock, _reason in clocks]
            bound = lb + rb
            deltas = sorted(rc(rrows[key]['t_ns']) - lc(lrows[key]['t_ns']) for key in paired)
        out['feeds'][feed] = {'matched_pairs': len(paired),
            'latency_result': 'unknown:' + unknown if unknown else (
                'measured_with_clock_bound' if deltas else 'unknown:no_pairs'),
            'p50_ms': statistics.median(deltas) if deltas else None,
            'p90_ms': deltas[math.ceil(.9 * len(deltas)) - 1] if deltas else None,
            'uncertainty_ms_max': bound if deltas else None,
            'left_definitely_earlier': sum(delta > bound for delta in deltas),
            'right_definitely_earlier': sum(delta < -bound for delta in deltas),
            'indeterminate': sum(abs(delta) <= bound for delta in deltas),
            'unpaired_left': len(lk - rk), 'unpaired_right': len(rk - lk),
            'excluded_left': len({row['sig'] for row in left_events if row.get('k') == 'tx'
                and row.get('feed') == feed and (feed, row['sig']) not in lrows}),
            'excluded_right': len({row['sig'] for row in right_events if row.get('k') == 'tx'
                and row.get('feed') == feed and (feed, row['sig']) not in rrows}),
            'identity_mismatch': len(lk & rk) - len(paired)}
    return out


def load_capture(path):
    path = Path(path)
    if (path / 'capture.json').stat().st_size > 1024 * 1024 or (
            path / 'events.jsonl').stat().st_size > 64 * 1024 * 1024:
        raise ValueError('capture exceeds read bounds')
    return json.loads((path / 'capture.json').read_text()), [json.loads(line)
        for line in (path / 'events.jsonl').read_text().splitlines()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run_args = sub.add_parser('run')
    run_args.add_argument('--host', required=True)
    run_args.add_argument('--owners-file', required=True)
    run_args.add_argument('--env-file', action='append', default=[], dest='env_files')
    run_args.add_argument('--clock-calibration-file', dest='calibration_file')
    run_args.add_argument('--seconds', type=float, default=120)
    run_args.add_argument('--max-messages', type=int, default=10000)
    run_args.add_argument('--out', required=True)
    compare = sub.add_parser('compare')
    compare.add_argument('left')
    compare.add_argument('right')
    args = vars(parser.parse_args())
    command = args.pop('command')
    if command == 'run':
        result = run(**args)
    else:
        left, le = load_capture(args['left'])
        right, re = load_capture(args['right'])
        result = compare_captures(left, le, right, re)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

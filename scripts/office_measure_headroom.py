#!/usr/bin/env python3
"""Small read-only host sample. Recommendations are explicit margins, not no-harm proof."""
import argparse
import json
import math
import os
from pathlib import Path
import time
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def recommend(*, memory_available, idle_cores):
    if (type(memory_available) is not int or memory_available <= 0
            or type(idle_cores) not in (int, float) or not math.isfinite(idle_cores) or idle_cores <= 0):
        raise ValueError("headroom_unknown")
    maximum = min(4 * 1024**3, int(memory_available * .10))
    return {"MemoryMax": maximum, "MemoryHigh": int(maximum * .75),
            "CPUQuota": min(50., idle_cores * 10.), "TasksMax": 128, "IOWeight": 1,
            "margin_rule": "reserve >=90% measured available RAM and idle CPU; ceiling 4GiB and half a CPU",
            "validation": "unproven until organic latency and containment drills"}


def _cpu():
    values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    return sum(values), values[3] + values[4]


def _available():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    raise ValueError("memory_unknown")


def pressure(kind):
    for line in Path('/proc/pressure/' + kind).read_text().splitlines():
        if line.startswith('some '):
            return float(dict(v.split('=') for v in line.split()[1:])['avg10']) / 100.
    raise ValueError("pressure_unknown")


def measure(*, samples=5, interval_s=.25):
    if type(samples) is not int or not 2 <= samples <= 20 or not .1 <= interval_s <= 1:
        raise ValueError("bounded_sample_required")
    memories, idle = [], []
    last = _cpu()
    for _ in range(samples):
        time.sleep(interval_s)
        current = _cpu()
        delta = current[0] - last[0]
        if delta <= 0:
            raise ValueError("cpu_unknown")
        idle.append((current[1] - last[1]) / delta * (os.process_cpu_count() or 1))
        memories.append(_available())
        last = current
    return {"at_host": time.time(), "samples": samples, "interval_s": interval_s,
            "memory_available_min": min(memories), "idle_cores_min": min(idle),
            "cpu_pressure": pressure('cpu'), "io_pressure": pressure('io'),
            "recommendation": recommend(memory_available=min(memories), idle_cores=min(idle)),
            "funded_entry_latency": "insufficient_sample", "funded_exit_latency": "insufficient_sample"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-out', type=Path)
    parser.add_argument('--clock-file', type=Path)
    parser.add_argument('--funded-heartbeat', type=Path)
    parser.add_argument('--funded-ledger', type=Path)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--interval-s', type=float, default=.25)
    args = parser.parse_args(argv)
    headroom=measure(samples=args.samples, interval_s=args.interval_s)
    if args.baseline_out:
        if any(p is None for p in (args.clock_file,args.funded_heartbeat,args.funded_ledger)):
            parser.error('baseline requires explicit clock-file, funded-heartbeat and funded-ledger')
        sample=baseline_sample(clock_file=args.clock_file,funded_heartbeat=args.funded_heartbeat,
                               funded_ledger=args.funded_ledger,now_host=time.time(),headroom=headroom)
        inserted=append_baseline(args.baseline_out,sample)
        print(json.dumps({'inserted':inserted,'sample_id':sample['sample_id'],'office_off':sample['office_off'],
                          'clock_state':sample['clock_state'],'output':str(args.baseline_out)},sort_keys=True))
        return
    print(json.dumps(headroom,sort_keys=True,indent=2,allow_nan=False))




def _utc(value):
    from datetime import datetime
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('clock_unknown')
    return at.timestamp()


def _quantile(values, q):
    values = sorted(values)
    at = (len(values) - 1) * q
    lo, hi = math.floor(at), math.ceil(at)
    return values[lo] + (values[hi] - values[lo]) * (at - lo)


def trace_tail(conn, *, now, window_s=3600., limit=500):
    """Submitted organic attempts only; stable IDs allow dedup across overlapping samples.

    Groups are chain x recorded lane x leg. Status is diagnostic, never a new group.
    Read limits describe coverage; absence in this tail does not prove global absence.
    """
    import sqlite3
    if type(limit) is not int or not 1 <= limit <= 1000 or not 0 < window_s <= 3600:
        raise ValueError('bounded_trace_window_required')
    groups, observations, read_rows, unknown_timings = {}, [], 0, 0
    try:
        for kind, query in (
            ('entry', 'SELECT attempt_id,network_id,signal_source,terminal_status,completed_at,total_ms,transaction_submitted_at '
                      'FROM memecoin_entry_attempts ORDER BY rowid DESC LIMIT ?'),
            ('exit', 'SELECT id,network_id,tier_variant,status,attempted_at,total_ms,sig '
                     'FROM memecoin_exit_execution_attempts ORDER BY rowid DESC LIMIT ?')):
            rows = conn.execute(query, (limit,)).fetchall()
            read_rows += len(rows)
            for identity, chain, lane, status, stamp, ms, submission in rows:
                if not isinstance(submission, str) or not submission.strip():
                    continue
                try:
                    source_at = _utc(stamp)
                    age = now - source_at
                except (ValueError, TypeError, AttributeError):
                    unknown_timings += 1
                    continue
                if not 0 <= age <= window_s:
                    continue
                if (type(ms) not in (int, float) or not math.isfinite(ms) or ms < 0
                        or identity is None or not str(identity)):
                    unknown_timings += 1
                    continue
                if kind == 'entry':
                    try:
                        if _utc(submission) > source_at:
                            unknown_timings += 1
                            continue
                    except (ValueError, TypeError, AttributeError):
                        unknown_timings += 1
                        continue
                groups.setdefault((kind, chain, lane), []).append(ms)
                observations.append(dict(trace_id=kind+':'+str(identity), leg=kind, network_id=chain,
                                         lane=lane, status=status, source_at=source_at, known_at=now, total_ms=ms))
    except sqlite3.Error:
        return None
    out = [dict(kind=key[0], network_id=key[1], lane=key[2], samples=len(values),
                p50_ms=_quantile(values, .5), p90_ms=_quantile(values, .9)) for key, values in groups.items()]
    worst = max(out, key=lambda row: row['p90_ms']) if out else dict(samples=0, p50_ms=None, p90_ms=None)
    return dict(samples=worst['samples'], p50_ms=worst['p50_ms'], p90_ms=worst['p90_ms'],
                groups=out, observations=observations, total_unique_attempts=len(observations),
                unknown_timings=unknown_timings, state='observed' if out else 'observed_empty', read_rows=read_rows,
                definition='submitted-attempt-total-ms-v2', coverage='bounded_tail', row_limit_per_leg=limit, window_s=window_s)


def read_json(path, limit=1048576):
    with open(path, 'r') as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('file_limit')
    return json.loads(raw)


def _readonly(path):
    # Observation-only handles never create a DB or pin a writer transaction.
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(str(path))
    conn.execute('PRAGMA busy_timeout=25')
    deadline = time.monotonic() + .025
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return conn




def office_off(*, proc_root=Path('/proc')):
    """Inspect command/cgroup metadata only. Disappearing processes are harmless; unreadable ones are unknown."""
    unknown=False
    processes=[p for p in Path(proc_root).iterdir() if p.name.isdigit()]
    if len(processes)>10000:
        return None
    for proc in processes:
        try:
            # Other users cannot own this user-service floor; don't require access to their command lines.
            status=proc/'status'
            if status.exists():
                uid_line=next((v for v in status.read_text().splitlines() if v.startswith('Uid:')),None)
                if uid_line is not None and int(uid_line.split()[1])!=os.getuid():
                    continue
            args=(proc/'cmdline').read_bytes().split(b'\0')
            group=(proc/'cgroup').read_text()
            if ('/office.slice' in group or any(v.endswith(b'/office_supervisor.py') or v==b'office_supervisor.py'
                    or b'office-research' in v for v in args)):
                return False
        except (OSError,ValueError):
            if proc.exists():unknown=True
    return None if unknown else True


def baseline_sample(*,clock_file,funded_heartbeat,funded_ledger,now_host,proc_root=Path('/proc'),headroom=None):
    import sqlite3
    result=dict(headroom or {},at_host=now_host,corrected_at=None,clock_state='unknown',offset_s=None,
                office_off=office_off(proc_root=proc_root),funded_heartbeat_age_s=None,
                funded_bracket_age_s=None,funded_auth_ok=None,latency=None)
    try:
        clock=read_json(clock_file)
        offset,at=clock['offset_s'],clock['at']
        if (type(offset) in (int,float) and type(at) in (int,float) and math.isfinite(offset)
                and math.isfinite(at) and 0<=now_host-at<=3600):
            result.update(corrected_at=now_host-offset,clock_state='known',offset_s=offset,offset_sampled_at=at)
    except (OSError,ValueError,KeyError,TypeError):
        pass
    try:
        beat=read_json(funded_heartbeat)
        for source,target in (('ts','funded_heartbeat_age_s'),('bracket_ts','funded_bracket_age_s')):
            at=beat.get(source)
            if type(at) in (int,float) and math.isfinite(at) and 0<=now_host-at:
                result[target]=now_host-at
        result['funded_auth_ok']=beat.get('auth_ok') if type(beat.get('auth_ok')) is bool else None
    except (OSError,ValueError,TypeError,AttributeError):
        pass
    if result['corrected_at'] is not None:
        try:
            from contextlib import closing
            with closing(_readonly(funded_ledger)) as conn:
                result['latency']=trace_tail(conn,now=result['corrected_at'])
        except (OSError,ValueError,sqlite3.Error):
            pass
    instant=result['corrected_at']
    result['sample_id']=('utc-minute:' if instant is not None else 'host-minute-unknown:')+str(int((instant if instant is not None else now_host)//60))
    result['schema']='office-host-baseline-v2'
    return result


def append_baseline(path,sample):
    """One locked append per minute. Never touches a source DB or rewrites a recorded sample."""
    import fcntl
    payload=json.dumps(sample,sort_keys=True,separators=(',',':'),allow_nan=False).encode()+b'\n'
    if len(payload)>524288:
        raise ValueError('baseline_sample_too_large')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as fh:
        fcntl.flock(fh,fcntl.LOCK_EX)
        size=fh.seek(0,os.SEEK_END)
        if size:
            fh.seek(max(0,size-1048576))
            lines=fh.read().splitlines()
            if lines and json.loads(lines[-1]).get('sample_id')==sample['sample_id']:
                return False
        fh.seek(0,os.SEEK_END);fh.write(payload);fh.flush();os.fsync(fh.fileno())
    return True


if __name__=='__main__':
    main()

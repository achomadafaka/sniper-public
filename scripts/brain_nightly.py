#!/usr/bin/env python3
"""Flag-only brain maintenance; --dry-run writes no graph or health state.

Exit: 0 success, 3 already running, 4 failed (local health records partial progress).
"""
import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot.brain_graph import live, nightly


def write_local(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with tmp.open('w') as file:
        json.dump(payload, file, allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(store, repo, *, check_fn, now_iso, rules=None, dry_run=False, secret_values=frozenset(), progress=None):
    progress = progress if progress is not None else {}
    started = time.monotonic()
    progress['stage'] = 'load_facts'
    facts = nightly.all_live_facts(store)
    progress['facts_live'] = len(facts)
    ev = dict(checks={}, flips=[], recovered_candidates=[])
    progress['evidence'] = ev
    progress['stage'] = 'evidence'
    if dry_run:
        would_break = []
        for fact in facts:
            checked = nightly.checked_evidence(fact.get('evidence') or [], check_fn)
            new, action = nightly.recheck(fact, checked, now_iso)
            if action == 'break':
                would_break.append(fact['id'])
            for item in new:
                key = f"{item.get('type')}:{item['check']}"
                ev['checks'][key] = ev['checks'].get(key, 0) + 1
        metrics = nightly.health_metrics(facts, ev, {'dry_run': True}, nightly.queue_report(facts, now_iso), now_iso)
        metrics.update(would_break=would_break, duration_s=round(time.monotonic() - started, 3))
        return metrics
    nightly.evidence_pass(store, facts, check_fn, now_iso, progress=ev)
    facts = nightly.all_live_facts(store)
    progress['stage'] = 'candidates'
    sweep = nightly.sweep_candidates(store, facts)
    if not dry_run:
        sweep['orphan_pending_cleared'] = len(nightly.clear_orphan_pending(store, facts))
        sweep['disputes_resolved'] = len(nightly.resolve_closed_disputes(store, facts))
    progress['candidates'] = sweep
    progress['stage'] = 'doctrine'
    doctrine = nightly.doctrine_drift(store, facts, rules, repo=repo, secret_values=secret_values) \
        if rules is not None else {'skipped': 'no doctrine rules supplied'}
    progress['doctrine'] = doctrine
    facts = nightly.all_live_facts(store)
    progress['stage'] = 'summaries'
    summary = nightly.summaries(store, facts, now_iso)
    metrics = nightly.health_metrics(facts, ev, sweep, nightly.queue_report(facts, now_iso), now_iso)
    metrics.update(doctrine=doctrine, summaries=summary, facts_per_section=summary['facts_per_section'],
                   duration_s=round(time.monotonic() - started, 3))
    progress.update(metrics, stage='health')
    store.insert_health(metrics)
    progress['stage'] = 'complete'
    return metrics


def main(argv=None, *, store_factory=None, state=None):
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument('--dry-run', action='store_true')
    flags.add_argument('--restore', type=Path)
    args = parser.parse_args(argv)
    state = Path(state) if state is not None else live.state_root(ROOT)
    local = state / 'data/brain_health_latest.json'
    lock = state / 'data/brain_nightly.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('brain nightly: another run holds the lock')
            return 3
        progress = {}
        try:
            if store_factory is None:
                from sniper.util.envfiles import load_env_files
                load_env_files(state)
                from bot.brain_graph.store import RestStore
                store = RestStore(timeout=10)
            else:
                store = store_factory()
            if args.restore:
                payload = json.loads(args.restore.read_text())
                metrics = payload['metrics']
                flips = metrics.get('flips', (metrics.get('evidence') or {}).get('flips', []))
                print(json.dumps(nightly.restore_flips(store, flips)))
                return 0
            from bot.brain_graph.evidence import check_all, fetch_once
            from bot.brain_graph.gate import secret_values_from_env, secret_values_from_mapping
            from bot.brain_graph.import_sources import doctrine_rules
            fetched = fetch_once(ROOT)
            # Paths depend on their cited commits. Cache that context without losing the bundle;
            # debounce annotations remain per fact.
            checked = {}
            def check(items):
                shas = tuple(sorted(str(e.get('ref')) for e in items if e.get('type') == 'sha'))
                def key(item):
                    return (item.get('type'), item.get('ref'),
                            shas if item.get('type') in ('path', 'doc') else ())
                if any(key(item) not in checked for item in items):
                    for result in check_all(items, ROOT, fetched=fetched):
                        checked.setdefault(key(result), result)
                out = []
                for item in items:
                    result = checked.get(key(item), {})
                    out.append(dict(item, check=result.get('check', 'unchecked'),
                                    check_reason=result.get('check_reason', 'check unavailable')))
                return out
            rules = [rule for source in ('CLAUDE.md', 'AGENTS.md')
                     if (ROOT / source).is_file()
                     for rule in doctrine_rules((ROOT / source).read_text(), source)]
            metrics = run(store, ROOT, check_fn=check, now_iso=datetime.now(timezone.utc).isoformat(),
                          rules=rules, dry_run=args.dry_run,
                          secret_values=(secret_values_from_env(state / '.env')
                                         | secret_values_from_env(state / '.fomo.env')
                                         | secret_values_from_mapping(os.environ)), progress=progress)
        except Exception as exc:
            if not args.dry_run:
                write_local(local, {'written_ts': time.time(), 'error': type(exc).__name__, 'metrics': progress})
            print(json.dumps({'error': type(exc).__name__, 'stage': progress.get('stage')}))
            return 4
        if not args.dry_run:
            write_local(local, {'written_ts': time.time(), 'metrics': metrics})
        print(json.dumps({key: metrics.get(key) for key in
                          ('facts_live', 'share_verified', 'disputed', 'evidence_failure_rate', 'duration_s')}))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())

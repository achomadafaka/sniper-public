#!/usr/bin/env python3
"""Import frozen legacy knowledge; dry-run executes the same writer against an isolated FakeStore."""
import argparse
from functools import lru_cache
import json
import os
import re
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MEMORY_DIR = Path.home() / '.claude/projects/-home-bigpapi-Desktop-Sniper/memory'


def read_snapshot(path):
    data = json.loads(Path(path).read_text())
    rows = data if isinstance(data, list) else data.get('rows', data.get('memories'))
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('snapshot must contain a list of memory rows')
    return rows


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            fd = None
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd is not None:
            os.close(fd)


def seed_import(store):
    from scripts.brain_seed import ROLE_VIEWS, seed
    result = seed(store)
    sections = {r['slug']: r for r in store.sections()}
    if 'crew/methods' not in sections:
        store.insert('brain_sections', dict(slug='crew/methods', title='Working methods',
                                           parent_id=sections['crew']['id']))
        result['sections_created'] += 1
    result.setdefault('views_updated', 0)
    for role in ROLE_VIEWS:
        view = store.view('role/' + role)
        spec = dict(view['query_spec'])
        if 'crew/methods' not in spec['sections']:
            spec['sections'] = [*spec['sections'], 'crew/methods']
            store.patch_view(view['name'], {'query_spec': spec})
            result['views_updated'] += 1
    return result


def git(repo, *args):
    return subprocess.run(['git','-C',str(repo),*args], check=True, capture_output=True,
                          text=True, timeout=60).stdout


def read_sources(repo, memory_dir, rows, ref):
    from bot.brain_graph.import_manifest import Sources
    if not memory_dir.is_dir():
        raise FileNotFoundError(memory_dir)
    files = {p.name: p.read_text() for p in sorted(memory_dir.glob('*.md'))}
    skill_path = '.claude/skills/handover/SKILL.md'
    skill = git(repo, 'show', ref + ':' + skill_path)
    blame, info, line_no = {}, {}, None
    for line in git(repo, 'blame', '--line-porcelain', ref, '--', skill_path).splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[1].isdigit() and parts[2].isdigit():
            line_no, info = int(parts[2]), {}
        elif line.startswith('\t'):
            from datetime import datetime, timezone
            stamp = info.get('author-time')
            date = datetime.fromtimestamp(int(stamp), timezone.utc).isoformat() if stamp else None
            blame[line_no] = (date, info.get('summary', ''))
        elif ' ' in line:
            key, value = line.split(' ', 1)
            info[key] = value
    doctrine = {name: (git(repo, 'show', ref + ':' + name),
                            git(repo, 'log', '-1', '--format=%cI', ref, '--', name).strip())
                for name in ('CLAUDE.md','AGENTS.md')}
    return Sources(handoff=files.pop('HANDOFF.md',''),
                   handoff_archive=files.pop('HANDOFF-archive-2026-08.md',''),
                   memory_files=files, supabase_rows=rows, skill=skill, skill_blame=blame,
                   doctrine=doctrine)


def build_manifest(repo, sources, secrets, *, handles=(), ref='origin/main', fetched=False):
    from bot.brain_graph import import_entities as E, import_manifest as M, evidence
    texts = [sources.handoff, sources.handoff_archive, sources.skill, *sources.memory_files.values(),
             *(t for t, date in sources.doctrine.values()), json.dumps(sources.supabase_rows)]
    tokens = {token for text in texts for token in E.HEX.findall(text)}
    commits = E.resolve_commits(tokens, repo)
    tables = set(json.loads((repo/'index/schema_registry.json').read_text())['tables'])
    @lru_cache(maxsize=None)
    def checked(kind, value):
        return evidence.check(dict(type=kind, ref=value), repo, ref=ref, fetched=fetched)[0]
    def broken(text):
        refs = []
        claimed = set(re.findall(r'(?i)\b(?:commit|sha|revision|merged|reverted|deployed)\s*[:=]?\s*[`\"\']?([0-9a-f]{7,40})\b', text))
        sha_refs = [token for token in E.HEX.findall(text) if token in commits or token in claimed]
        for kind, values in [('sha', sha_refs), ('path', E.PATH.findall(text)),
                             ('knob', E.KNOB.findall(text))]:
            for value in dict.fromkeys(values):
                if checked(kind, value) == 'broken':
                    refs.append(dict(type=kind, ref=value))
        return refs
    manifest = M.build(sources, commits=commits, tables=tables, handles=set(handles),
                   knob_ok=lambda name: checked('knob', name) == 'ok',
                   broken_refs_fn=broken, secret_values=secrets)
    for fact in manifest.facts:
        if not fact['import_status'] and any(checked(e['type'], e['ref']) == 'broken'
                for e in fact['draft']['evidence']):
            fact['import_status'] = 'evidence_broken'
            manifest.report['generated_ref_broken'] = manifest.report.get('generated_ref_broken', 0) + 1
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--snapshot', type=Path, required=True, help='frozen JSON memory rows')
    parser.add_argument('--source-ref', default='HEAD', help='pre-cutover Git ref for doctrine and handover skill')
    parser.add_argument('--memory-dir', type=Path, default=MEMORY_DIR)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--max-rps', type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.max_rps <= 0 or args.max_rps > 5:
        parser.error('--max-rps must be greater than zero and at most five')
    report, secrets = {}, set()
    stage = 'load_sources'
    try:
        from bot.brain_graph import live, importer
        from bot.brain_graph.gate import secret_values_from_env, secret_values_from_mapping
        from sniper.util.envfiles import load_env_files
        state = live.state_root(args.repo)
        load_env_files(str(state))
        secrets = secret_values_from_mapping(os.environ)
        for name in live.ENV_FILES:
            secrets |= secret_values_from_env(state/name)
        sources = read_sources(args.repo, args.memory_dir, read_snapshot(args.snapshot), args.source_ref)
        handles, handles_status = set(), 'unavailable'
        dbpath = state/'sniper_tracker.db'
        if dbpath.exists():
            from contextlib import closing
            from sniper.persistence.db_core import get_readonly_connection
            try:
                with closing(get_readonly_connection(str(dbpath))) as db:
                    handles = {r[0] for r in db.execute('SELECT DISTINCT handle FROM memecoin_wallets WHERE handle IS NOT NULL')}
                handles_status = 'read'
            except Exception:
                pass  # Handle hints may be unavailable; never inferred as an empty live population.
        from bot.brain_graph.evidence import fetch_once
        fetched = fetch_once(args.repo)
        stage = 'build_manifest'
        manifest = build_manifest(args.repo, sources, secrets, handles=handles, ref=args.source_ref, fetched=fetched)
        if args.dry_run:
            sys.path.insert(0, str(ROOT/'tests'))
            from brain_graph_fake_store import FakeStore
            store = FakeStore()
        else:
            from bot.brain_graph.store import RestStore
            store = importer._paced(RestStore(timeout=10), __import__('time').sleep, args.max_rps)
        stage = 'seed'
        seed_counts = seed_import(store)
        stage = 'write_manifest'
        report = importer.run(store, manifest, repo=args.repo, secret_values=secrets, max_rps=args.max_rps, fetched=fetched)
        report.update(mode='dry_run' if args.dry_run else 'live', source_ref=args.source_ref,
                      source_rows=len(sources.supabase_rows), source_files=len(sources.memory_files),
                      evidence_fetch_succeeded=fetched,
                      wallet_handles=handles_status, seed=seed_counts)
        write_report(args.report, report)
        print(json.dumps({k:v for k,v in report.items() if k not in ('memory_facts', 'refusal_samples')}, sort_keys=True))
        return 0 if not report['facts_refused'] and not report['redaction_changed'] else 2
    except Exception as exc:
        report.update(getattr(exc, 'report', {}))
        from bot.brain_graph.gate import redact
        import traceback
        report['error_type'] = type(exc).__name__
        report['error_stage'] = stage
        report['error_detail'] = redact(str(exc), secrets)[0][:1000]
        report['error_stack'] = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                                 for f in traceback.extract_tb(exc.__traceback__)]
        write_report(args.report, report)
        print(json.dumps({'error_type': type(exc).__name__}), file=sys.stderr)
        return 4


if __name__ == '__main__':
    sys.exit(main())

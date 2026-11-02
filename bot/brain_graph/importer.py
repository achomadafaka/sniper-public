"""Resumable import. UUIDs identify writes; source hashes identify immutable episode bodies."""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import time
import uuid

from bot.brain_graph import READ_STATUSES
from bot.brain_graph import api, import_sources as S
from bot.brain_graph.store import BrainRefused, BrainUnavailable, RestStore


def stable_id(kind, key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'sniper-brain-import:v1:' + kind + ':' + key))


class CachedStore:
    def __init__(self, store):
        self.store = store
        self._sections = store.sections()
        self.entities = {r['id']: r for r in store._all('brain_entities')}
        self.facts = {r['id']: r for r in store._all('brain_facts')}

    def __getattr__(self, name):
        return getattr(self.store, name)

    def get(self, table, ident):
        if table == 'brain_facts':
            return deepcopy(self.facts.get(ident))
        return self.store.get(table, ident)

    def sections(self, slugs=None):
        return [dict(s) for s in self._sections if not slugs or s['slug'] in slugs]

    def find_entity(self, kind, name):
        key = name.strip().lower()
        return next((dict(r) for r in self.entities.values() if r['type'] == kind and
                     (r['name'].strip().lower() == key or key in r.get('aliases', []))), None)

    def find_facts(self, entity_ids, kinds=None, live_only=True):
        ids = set(entity_ids)
        rows = [f for f in self.facts.values() if ids.intersection(f['entity_ids']) and
                (not kinds or f['kind'] in kinds) and (not live_only or
                    (f.get('valid_to') is None and f['status'] in READ_STATUSES))]
        return sorted((dict(f) for f in rows), key=lambda f: (f['recorded_at'], f['id']))

    def insert(self, table, row):
        try:
            result = self.store.insert(table, row)
        except BrainRefused as exc:
            if table != 'brain_entities' or not any(t in str(exc).lower() for t in ('duplicate','23505')):
                raise
            result = self.store.find_entity(row['type'], row['name'])
            if not result:
                raise
        if table == 'brain_entities':
            self.entities[result['id']] = result
        if table == 'brain_facts':
            self.facts[result['id']] = result
        return result

    def write_fact(self, row, actions):
        result = self.store.write_fact(row, actions)
        self.facts[result['id']] = result
        for a in actions:
            if a['verdict'] in ('supersedes', 'contradicts'):
                self.facts[a['id']] = self.store.get('brain_facts', a['id'])
        return result

    def patch_fact(self, ident, version, patch):
        result = self.store.patch_fact(ident, version, patch)
        self.facts[ident] = result
        return result

    def supersede(self, ident, version, row):
        result = self.store.supersede(ident, version, row)
        self.facts[result['id']] = result
        self.facts[ident] = self.store.get('brain_facts', ident)
        return result


def write_episodes(store, episodes, report):
    """Return complete key->parent IDs. Mismatched old redaction is reported, never overwritten."""
    for name in ('episodes_written','episodes_skipped','parts_completed','redaction_changed'):
        report.setdefault(name, 0)
    existing = defaultdict(list)
    for row in store._all('brain_episodes'):
        if row.get('source_ref'):
            existing[row['source_ref']].append(row)
    parents, firsts, later = {}, [], []
    for ep in episodes:
        key, body = ep['key'], ep['body']
        chunks = [body[i:i + api.PART] for i in range(0, max(1, len(body)), api.PART)]
        rows = existing[key]
        parts = {r['part_no']: r for r in rows}
        if len(parts) != len(rows) or any(n < 1 or n > len(chunks) or r['body'] != chunks[n-1]
                                         for n, r in parts.items()):
            report['redaction_changed'] += 1
            continue
        parent = parts.get(1, {}).get('id') or stable_id('episode', key + ':1')
        if any(r.get('parent_id') not in (None, parent) for r in rows):
            report['redaction_changed'] += 1
            continue
        missing = [n for n in range(1, len(chunks)+1) if n not in parts]
        if not missing:
            report['episodes_skipped'] += 1
        elif rows:
            report['parts_completed'] += len(missing)
        else:
            report['episodes_written'] += 1
        for n in missing:
            row = dict(id=parent if n == 1 else stable_id('episode', key + ':' + str(n)),
                       kind=ep['kind'], body=chunks[n-1], author=ep.get('author', 'import'),
                       source_ref=key, occurred_at=ep.get('occurred_at'), part_no=n,
                       parent_id=None if n == 1 else parent, session_id=S.IMPORT_SESSION)
            (firsts if n == 1 else later).append(row)
        parents[key] = parent
    for batch in (firsts, later):
        for offset in range(0, len(batch), 200):
            store.insert_many('brain_episodes', batch[offset:offset+200], ignore_duplicates=True)
    return parents


def queue_tasks(queue, *, import_day):
    groups = defaultdict(list)
    for item in queue:
        groups[item['section']].append(item)
    start = datetime.fromisoformat(import_day).replace(tzinfo=timezone.utc)
    out = []
    for section, items in sorted(groups.items()):
        items = sorted(items, key=lambda q: (q['episode_key'], q['excerpt']))
        for offset in range(0, len(items), 25):
            batch = items[offset:offset+25]
            detail = '\n'.join(i['episode_key'] + ' ' + S.normalize(i['excerpt'])[:120] for i in batch)
            digest = hashlib.sha256(detail.encode()).hexdigest()[:10]
            n = offset // 25 + 1
            out.append(dict(kind='task', author='import', author_session=S.IMPORT_SESSION, section=section,
                statement=f'Judge import batch {section} #{n} ({digest}): turn each item into a fact with brain_cli.py write, or drop it',
                detail=detail, evidence=[dict(type='episode', ref=i['episode_key']) for i in batch],
                episode_ids=[i['episode_key'] for i in batch],
                entities=[dict(type='task', name=f'import-judge:{section}:{digest}', section=section)],
                valid_from=start.isoformat(), verify_by=(start + timedelta(days=n)).isoformat()))
    return out


def _paced(store, sleep_fn, max_rps):
    if not isinstance(store, RestStore):
        return store
    last = [0.0]
    def request(*a, **kw):
        delay = 1 / max_rps - (time.monotonic() - last[0])
        if delay > 0:
            sleep_fn(delay)
        last[0] = time.monotonic()
        return store._request(*a, **kw)
    return RestStore(request=request, timeout=store._timeout)


def run(store, manifest, *, repo, secret_values, now_fn=None, sleep_fn=time.sleep, max_rps=5.0, fetched=False):
    if max_rps <= 0:
        raise ValueError('max_rps must be positive')
    now = (now_fn or (lambda: datetime.now(timezone.utc).isoformat()))()
    report = dict(episodes_written=0, episodes_skipped=0, parts_completed=0, redaction_changed=0,
        facts_written=0, facts_skipped=0, facts_refused={}, statuses={}, judge_pending=0,
        candidate_links=0, queue_tasks=0, links_written=0, links_present=0, stale_at_import=0,
        valid_from_unknown=0, window=[None,None], memory_facts={}, manifest=manifest.report)
    store = _paced(store, sleep_fn, max_rps)
    try:
        eps = write_episodes(store, manifest.episodes, report)
        cached = CachedStore(store)
        index = {next((e['import_key'] for e in f.get('evidence', []) if e.get('import_key')), S.normalize(f['statement'])): f for f in cached.facts.values()
                 if f.get('author_session') == S.IMPORT_SESSION}
        episode_rows = {r['id']: r for r in store._all('brain_episodes')}
        slots = defaultdict(list)
        for f in index.values():
            for ev in f.get('evidence', []):
                if str(ev.get('note', '')).startswith('import_slot:'):
                    ref = ev.get('ref')
                    anchor = episode_rows.get(ref, {}).get('source_ref', '').rsplit('#',1)[0] if ev['type'] == 'episode' else str(ref).split(':')[0]
                    slots[(anchor, ev['note'][12:])].append(f['id'])
        items = list(manifest.facts)
        resolved_queue = [dict(q, episode_key=eps[q['episode_key']]) for q in manifest.queue if q['episode_key'] in eps]
        for d in queue_tasks(resolved_queue, import_day=now[:10]):
            items.append(dict(key=S.normalize(d['statement']), draft=d, slot='', anchor='',
                              episode_key=None, same_source=False, is_queue=True))
        for item in items:
            key = item['key']
            if key in index:
                report['facts_skipped'] += 1
                continue
            d = deepcopy(item['draft'])
            epkey = item.get('episode_key')
            required_eps = {e['ref'] for e in d['evidence'] if e.get('type') == 'episode'} if epkey else set()
            if epkey and (epkey not in eps or not required_eps.issubset(eps)):
                report['facts_refused']['episode redaction changed'] = report['facts_refused'].get('episode redaction changed',0)+1
                continue
            if epkey:
                d['episode_ids'] = [eps[e['ref']] for e in d['evidence'] if e.get('type') == 'episode' and e['ref'] in eps]
            for ev in d['evidence']:
                if ev.get('type') == 'episode' and ev['ref'] in eps:
                    ev['ref'] = eps[ev['ref']]
            if d['evidence']:
                d['evidence'][0]['import_key'] = key
                if item.get('slot'):
                    d['evidence'][0]['note'] = 'import_slot:' + item['slot']
            if not d.get('valid_from'):
                d['valid_from'] = now
                report['valid_from_unknown'] += 1
            d.setdefault('verify_by', S.verify_by(d['kind'], d['valid_from'], key))
            same = {}
            # Structural refinement is only a bullet's relationship to its source headline.
            # Siblings may contradict each other and still need semantic judgement.
            if item.get('same_source') and epkey:
                same = {f['id']: 'refines' for f in index.values()
                        if f.get('valid_to') is None and f['status'] in READ_STATUSES
                        and any(e.get('type') == 'episode' and e.get('ref') == eps[epkey]
                                and e.get('note') == 'import_slot:headline' for e in f.get('evidence', []))}
            prior = sorted({ident for src in item.get('sources', [item])
                            for ident in slots.get((src.get('anchor'), src.get('slot')), [])})
            result = api.write_fact(cached, d, repo=repo, interactive=False, judgements=same,
                secret_values=secret_values, fetched=fetched, import_status=item.get('import_status'),
                valid_to=item.get('valid_to'), judge_pending=item.get('judge_pending', False),
                candidate_ids=prior, write_id=stable_id('fact', key))
            if result['status'] != 'written':
                # Refusal reasons may include raw evidence refs; only aggregate safe categories.
                reason = 'secret' if any('secret' in str(e) for e in result.get('errors', [])) else 'gate'
                report['facts_refused'][reason] = report['facts_refused'].get(reason,0)+1
                from bot.brain_graph.gate import redact
                report.setdefault('refusal_samples', []).append(dict(
                    write_id=stable_id('fact', key), errors=[redact(str(e), secret_values)[0][:500]
                    for e in result.get('errors', [])[:4]]))
                continue
            f = result['fact']
            index[key] = f
            report['facts_written'] += 1
            report['queue_tasks'] += bool(item.get('is_queue'))
            report['window'][0] = report['window'][0] or f['recorded_at']
            report['window'][1] = f['recorded_at']
        existing_links = {(l['from_id'],l['to_id'],l['rel']) for l in store._all('brain_links')}
        for link in manifest.links:
            if link['from_key'] not in index or link['to_key'] not in index:
                continue
            frm, to = index[link['from_key']]['id'], index[link['to_key']]['id']
            triple = (frm,to,link['rel'])
            if triple in existing_links:
                report['links_present'] += 1
                continue
            store.insert_many('brain_links', [dict(id=stable_id('link', ':'.join(triple)),
                from_id=frm, from_kind='fact', to_id=to, to_kind='fact', rel=link['rel'], author='import')], ignore_duplicates=True)
            report['links_written'] += 1
            existing_links.add(triple)
        relevant = [index[k] for k in {i['key'] for i in items} if k in index]
        report['statuses'] = dict(Counter(f['status'] for f in relevant))
        report['facts_total'] = len(relevant)
        report['judge_pending'] = sum(bool(f.get('judge_pending')) for f in relevant)
        report['stale_at_import'] = sum(f.get('verify_by','infinity') < now for f in relevant)
        ids = {f['id'] for f in relevant}
        report['candidate_links'] = sum(frm in ids and rel == 'candidate' for frm,to,rel in existing_links)
        report['memory_facts'] = {name: sorted({index[k]['id'] for k in keys if k in index})
                                  for name, keys in manifest.memory_keys.items()}
        return report
    except (BrainUnavailable, BrainRefused) as exc:
        exc.report = report
        raise

"""Deterministic, side-effect-free import manifest; provenance first, facts never self-verified."""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json

from bot.brain_graph import import_entities as E, import_sources as S
from bot.brain_graph.gate import redact


@dataclass
class Sources:
    handoff: str = ''
    handoff_archive: str = ''
    memory_files: dict = field(default_factory=dict)
    supabase_rows: list = field(default_factory=list)
    skill: str = ''
    skill_blame: dict = field(default_factory=dict)
    doctrine: dict = field(default_factory=dict)


@dataclass
class Manifest:
    episodes: list = field(default_factory=list)
    facts: list = field(default_factory=list)
    queue: list = field(default_factory=list)
    links: list = field(default_factory=list)
    report: dict = field(default_factory=dict)
    memory_keys: dict = field(default_factory=dict)


def build(sources, *, commits, tables, handles, knob_ok, broken_refs_fn, secret_values):
    m = Manifest()
    counts, seen, queue_seen = Counter(), {}, set()
    def clean(text):
        body, n = redact(str(text or ''), secret_values)
        counts['redactions'] += n
        return body
    def episode(anchor, original, kind, date=None, author='import'):
        key = S.episode_key(anchor, original)
        if not any(e['key'] == key for e in m.episodes):
            m.episodes.append(dict(key=key, anchor=anchor, body=clean(original), kind=kind,
                                   occurred_at=date, author=author))
        return key
    def queue(section, key, excerpt):
        pair = (key, S.normalize(clean(excerpt))[:120])
        if pair not in queue_seen:
            m.queue.append(dict(section=section, episode_key=key, excerpt=pair[1]))
            queue_seen.add(pair)
    def fact(text, *, anchor, slot, section, kind, date, ep=None, author='import', status=None,
             valid_to=None, pending=False, same_source=False, broken=(), memory_name=None, evidence=None):
        statement, detail = S.split_statement(clean(text))
        if len(statement) < 8:
            counts['short_statement_episode_only'] += 1
            return None
        key = S.normalize(statement)
        if status in ('retracted', 'superseded'):
            key += '\n[closed:' + status + ':' + anchor + ']'
        if memory_name:
            m.memory_keys.setdefault(memory_name, []).append(key)
        if key in seen:
            counts['duplicate_statement'] += 1
            prior = seen[key]
            if ep:
                ev = dict(type='episode', ref=ep, note='import_slot:' + slot)
                if ev not in prior['draft']['evidence']:
                    prior['draft']['evidence'].append(ev)
            ent = dict(type='path', name=anchor, section=section)
            if ent not in prior['draft']['entities']:
                prior['draft']['entities'].append(ent)
            prior['sources'].append(dict(anchor=anchor, slot=slot))
            prior['judge_pending'] |= pending
            if broken and not prior['import_status']:
                prior['import_status'] = 'evidence_broken'
            prior['draft']['evidence'].extend(b for b in broken if b not in prior['draft']['evidence'])
            return key
        ents = E.extract(statement + ' ' + (detail or ''), commits=commits, tables=tables,
                         handles=handles, knob_ok=knob_ok)
        ev = list(evidence or ([dict(type='episode', ref=ep, note='import_slot:' + slot)] if ep else []))
        ev += E.evidence_items(ents)
        ev += [b for b in broken if b not in ev]
        draft = dict(statement=statement, detail=detail, kind=kind, author=author,
                     author_session=S.IMPORT_SESSION, section=section, evidence=ev,
                     entities=[dict(type='path', name=anchor, section=section)] + ents)
        if date:
            draft.update(valid_from=date, verify_by=S.verify_by(kind, date, key))
        m.facts.append(dict(key=key, slot=slot, anchor=anchor, draft=draft,
                            import_status=status or ('evidence_broken' if broken else None),
                            valid_to=valid_to, judge_pending=pending, episode_key=ep,
                            same_source=same_source, sources=[dict(anchor=anchor, slot=slot)]))
        seen[key] = m.facts[-1]
        return key

    for anchor, text in [('HANDOFF.md', sources.handoff), ('HANDOFF-archive-2026-08.md', sources.handoff_archive)]:
        blocks = S.handoff_blocks(text)
        dates = S.block_dates(blocks)
        newest = max((d for d in dates.values() if d), default=None)
        cutoff = (datetime.fromisoformat(newest) - timedelta(days=7)).date().isoformat() if newest else None
        for line, header, body in blocks:
            date = S._iso(header) or S._iso(dates.get(line))
            key = episode(anchor, '\n'.join([header] + body), 'handoff_line', date,
                          S.handoff_author(header) or 'import')
            for candidate in S.split_candidates(header, body):
                kind = S.classify(candidate)
                if kind in ('rule','retraction') or (kind == 'decision' and date and cutoff and date[:10] >= cutoff):
                    queue(E.fact_section(S.front_of(candidate), None), key, candidate)

    memories = []
    local_names = {}
    for filename, body in sorted(sources.memory_files.items()):
        if filename == 'MEMORY.md' or filename.startswith('HANDOFF'):
            continue
        parsed = S.memory_source(filename, body)
        name = S.frontmatter(body).get('name') or filename.removesuffix('.md')
        local_names[name] = name
        local_names[filename.removesuffix('.md')] = name
        memories.append((name, 'memory/' + filename, body, parsed, None, None))
    direct_names = {}
    for row in sorted(sources.supabase_rows, key=lambda r: str(r.get('name') or '')):
        name = str(row.get('name') or row.get('id'))
        suffix = name.rsplit('/', 1)[-1].removesuffix('.md')
        if suffix in local_names or (name.startswith('session/HANDOFF') and (sources.handoff or sources.handoff_archive)):
            counts['supabase_local_duplicates'] += 1
            if suffix in local_names:
                direct_names[name] = local_names[suffix]
                for i, item in enumerate(memories):
                    if item[0] == local_names[suffix]:
                        parsed = dict(item[3])
                        if not parsed['date']:
                            parsed['date'] = S._iso(row.get('updated_at'))
                            if parsed['date']:
                                counts['mirror_date_fallback'] += 1
                        status, when = item[4:]
                        if row.get('status') in ('retired','superseded','retracted'):
                            status = 'retracted' if row['status'] == 'retracted' else 'superseded'
                            when = S._iso(row.get('updated_at')) or parsed['date']
                        memories[i] = (*item[:3], parsed, status, when)
            continue
        body = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        date = S._iso(row.get('updated_at'))
        parsed = dict(headline=S.normalize(row.get('description') or name),
                      bullets=S.apply_rules(str(row.get('content') or '')),
                      kind='rule' if row.get('type') == 'feedback' else 'finding', type=row.get('type'),
                      date=date, marker='marked' if S.STALE_RX.search(name + ' ' + str(row.get('description') or '')) else None)
        status = 'superseded' if row.get('status') in ('retired','superseded') else 'retracted' if row.get('status') == 'retracted' else None
        memories.append((name, 'supabase:memories/' + name, body, parsed, status, date if status else None))
    for name, anchor, body, parsed, status, valid_to in memories:
        ep = episode(anchor, body, 'legacy_memory', parsed['date'])
        section = E.fact_section(S.front_of(name + ' ' + parsed['headline']), parsed['type'])
        if parsed['marker']:
            queue(section, ep, parsed['headline'])
        for candidate in S.split_candidates('', S.strip_frontmatter(body).splitlines()):
            if S.classify(candidate) == 'retraction':
                queue(section, ep, candidate)
        if parsed['type'] == 'user' or anchor.rsplit('/',1)[-1].startswith('topic-'):
            counts['episodes_only'] += 1
            continue
        broken = broken_refs_fn(clean(body))
        # Headline before its bullets keeps derived_from direction honest. Doctrine and traps
        # still land last, so the bounded newest-first boot prioritises operational knowledge.
        fact(parsed['headline'], anchor=anchor, slot='headline', section=section, kind=parsed['kind'],
             date=parsed['date'], ep=ep, status=status, valid_to=valid_to, pending=bool(parsed['marker']),
             broken=broken, memory_name=name)
        for n, bullet in enumerate(parsed['bullets']):
            fact(bullet, anchor=anchor, slot='bullet:' + str(n), section=section, kind='rule',
                 date=parsed['date'], ep=ep, status=status, valid_to=valid_to, pending=bool(parsed['marker']),
                 same_source=True, broken=broken, memory_name=name)
    for alias, canonical in direct_names.items():
        m.memory_keys[alias] = list(m.memory_keys.get(canonical, []))
    for source, (text, date) in sorted(sources.doctrine.items()):
        for rule in S.doctrine_rules(text, source):
            fact(rule['statement'], anchor=source, slot='line:' + str(rule['line']), section='brain/doctrine',
                 kind='rule', date=S._iso(date), author='operator', evidence=[dict(type='path', ref=f"{source}:{rule['line']}")])
    if sources.skill:
        for kind, items in [('rule', S.standing_rules(sources.skill)), ('trap', S.traps(sources.skill))]:
            for line, text in items:
                date, _subject = sources.skill_blame.get(line, (None, ''))
                ep = episode('SKILL.md', text, 'doc', S._iso(date))
                fact(text, anchor='.claude/skills/handover/SKILL.md', slot=f'{kind}:{line}',
                     section='brain/doctrine', kind=kind, date=S._iso(date), ep=ep)
    row_names = {str(r.get('id')): r.get('name') for r in sources.supabase_rows}
    for row in sources.supabase_rows:
        if row.get('superseded_by'):
            old_keys = m.memory_keys.get(row.get('name'), [])
            target = row_names.get(str(row['superseded_by']), str(row['superseded_by']))
            new_keys = m.memory_keys.get(target, [])
            if old_keys and new_keys and old_keys[0] != new_keys[0]:
                m.links.append(dict(from_key=new_keys[0], to_key=old_keys[0], rel='supersedes'))
    m.report = dict(counts)
    return m

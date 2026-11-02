"""Flag-only graph maintenance. No automatic verification or lexical doctrine replacement."""
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations

from bot.brain_graph import READ_STATUSES, group_of
from bot.brain_graph.contradict import keys
from bot.brain_graph.store import BrainRefused, VersionConflict

BREAK_AFTER = 2


def _live(fact):
    return fact.get('valid_to') is None and fact.get('status') in READ_STATUSES


def all_live_facts(store, page=500):
    page = max(1, min(int(page), 500))
    out, after = [], None
    while True:
        rows = store.live_facts_page(after, page)
        out.extend(rows)
        if len(rows) < page:
            return out
        if rows[-1]['id'] == after:
            raise BrainRefused('paging did not advance')
        after = rows[-1]['id']


def _signature(items):
    return [(e.get('type'), e.get('ref'), e.get('check'), e.get('check_reason'),
             e.get('broken_runs', 0)) for e in items or []]


def recheck(fact, checked, now_iso):
    """Only this run's broken result can trip debounce; unknown leaves its count intact."""
    results = {(e.get('type'), e.get('ref')): e for e in checked}
    out, broken = [], False
    for previous in fact.get('evidence') or []:
        result = results.get((previous.get('type'), previous.get('ref')), {})
        verdict = result.get('check', 'unchecked')
        if verdict not in ('ok', 'broken', 'unchecked', 'pending_push'):
            verdict = 'unchecked'
        if previous.get('type') == 'query':
            verdict = 'unchecked'
        runs = max(0, int(previous.get('broken_runs') or 0))
        if verdict == 'broken' and previous.get('checked_at') != now_iso:
            runs += 1
        elif verdict in ('ok', 'pending_push'):
            runs = 0
        broken |= verdict == 'broken' and runs >= BREAK_AFTER
        out.append(dict(previous, check=verdict, broken_runs=runs, checked_at=now_iso,
                        check_reason=result.get('check_reason', 'check unavailable')))
    if broken and _live(fact) and fact['status'] != 'evidence_broken':
        return out, 'break'
    return out, 'annotate' if _signature(out) != _signature(fact.get('evidence')) else 'none'


def checked_evidence(items, check_fn):
    items = [e for e in items if e.get('type') != 'query']
    try:
        result = check_fn(items) if items else []
        return [e for e in result if isinstance(e, dict)] if isinstance(result, list) else []
    except Exception as exc:
        return [dict(e, check='unchecked', check_reason=type(exc).__name__) for e in items]


def evidence_pass(store, facts, check_fn, now_iso, *, progress=None):
    out = progress if progress is not None else {}
    out.update(checks={}, flips=[], patched=0, version_conflict=0, recovered_candidates=[])
    for fact in facts:
        if not _live(fact) or fact['kind'] == 'observation' or not fact.get('evidence'):
            continue
        checked = checked_evidence(fact['evidence'], check_fn)
        new, action = recheck(fact, checked, now_iso)
        for e in new:
            key = f"{e.get('type')}:{e['check']}"
            out['checks'][key] = out['checks'].get(key, 0) + 1
        if fact['status'] == 'evidence_broken' and all(e['check'] == 'ok' for e in new):
            out['recovered_candidates'].append(fact['id'])
        if action == 'none':
            continue
        patch = {'evidence': new}
        if action == 'break':
            patch['status'] = 'evidence_broken'
        try:
            saved = store.patch_fact(fact['id'], fact['version'], patch)
        except VersionConflict:
            out['version_conflict'] += 1
            continue
        out['patched'] += 1
        if action == 'break':
            out['flips'].append(dict(id=fact['id'], prior_status=fact['status'],
                                     flipped_version=saved['version']))
    return out


def restore_flips(store, flips):
    """Explicit operator rollback only; never overwrite work performed after the flip."""
    out = dict(restored=0, skipped=0, refused=0)
    for flip in flips:
        current = store.get('brain_facts', flip['id'])
        if (not current or not _live(current) or current['status'] != 'evidence_broken'
                or current['version'] != flip.get('flipped_version')):
            out['skipped'] += 1
            continue
        try:
            store.patch_fact(current['id'], current['version'], {'status': flip['prior_status']})
            out['restored'] += 1
        except (BrainRefused, VersionConflict):
            out['refused'] += 1
    return out


def sweep_candidates(store, facts, author='cron', *, pair_budget=500):
    """Queue bounded work atomically; resume both missing links and interrupted old flags."""
    by_id = {f['id']: f for f in facts if _live(f)}
    ids = list(by_id)
    links, judged = store.links_among(ids), store.judged_pairs(ids)
    linked, repair = set(), []
    for link in links:
        if link.get('from_kind') != 'fact' or link.get('to_kind') != 'fact':
            continue
        a, b = link['from_id'], link['to_id']
        pair = tuple(sorted((a, b)))
        linked.add(pair)
        if (link['rel'] == 'candidate' and pair not in judged and a in by_id and b in by_id
                and not by_id[a].get('judge_pending')):
            repair.append((a, b))
    buckets = defaultdict(dict)
    for f in by_id.values():
        group = group_of(f['kind'])
        if group not in ('normative', 'descriptive'):
            continue
        for entity in f.get('entity_ids') or []:
            for key in keys(f['statement']):
                buckets[(entity, group, key)][f['id']] = f
    out = dict(pairs_new=0, pairs_seen=0, facts_flagged=0, version_conflict=0, budget_exhausted=False)
    flagged, seen, attempts = set(), set(), 0

    def queue(newer, older, existing):
        nonlocal attempts
        if attempts >= pair_budget:
            out['budget_exhausted'] = True
            return False
        attempts += 1
        if store.queue_candidate(newer, older, author=author):
            out['pairs_new'] += not existing
            if not by_id[newer].get('judge_pending'):
                flagged.add(newer)
                by_id[newer] = dict(by_id[newer], judge_pending=True)
        return True

    for newer, older in repair:
        if not queue(newer, older, True):
            break
    if not out['budget_exhausted']:
        for members in buckets.values():
            ordered = sorted(members.values(), key=lambda f: (f.get('recorded_at') or '', f['id']))
            for older, newer in combinations(ordered, 2):
                pair = tuple(sorted((older['id'], newer['id'])))
                if pair in seen:
                    continue
                seen.add(pair)
                if pair in linked or pair in judged:
                    continue
                if not queue(newer['id'], older['id'], False):
                    break
            if out['budget_exhausted']:
                break
    out.update(pairs_seen=len(seen), facts_flagged=len(flagged))
    return out


def clear_orphan_pending(store, facts):
    """Clear judge_pending on a live fact that has no unjudged candidate pair with a live fact left
    (Gamma 2026-09-29: 4 facts stuck pending with no link; verify refused them and judge had no
    pair). Returns the ids cleared. A version conflict leaves the flag for the next night."""
    by_id = {f['id']: f for f in facts if _live(f)}
    pending = [i for i, f in by_id.items() if f.get('judge_pending')]
    if not pending:
        return []
    judged = store.judged_pairs(list(by_id))
    open_pairs = set()
    for link in store.links_among(list(by_id)):
        if link.get('rel') != 'candidate' or link.get('from_kind') != 'fact' or link.get('to_kind') != 'fact':
            continue
        a, b = link['from_id'], link['to_id']
        if a in by_id and b in by_id and tuple(sorted((a, b))) not in judged:
            open_pairs.update((a, b))
    cleared = []
    for ident in pending:
        if ident in open_pairs:
            continue
        f = by_id[ident]
        try:
            store.patch_fact(ident, f['version'], {'judge_pending': False})
            cleared.append(ident)
        except Exception:                   # version conflict or outage: next night retries
            continue
    return cleared


CLOSED = ('superseded', 'retracted', 'refuted')


def resolve_closed_disputes(store, facts):
    """A disputed fact whose every 'contradicts' counterpart is closed goes back to unverified
    (Gamma 2026-09-29 did this by hand for a9ba913b; judge refuses a second verdict on a judged
    pair). Returns the ids resolved. A version conflict leaves it for the next night."""
    disputed = {f['id']: f for f in facts if f.get('status') == 'disputed'}
    if not disputed:
        return []
    others = {i: set() for i in disputed}
    for link in store.links_among(list(disputed)):
        if link.get('rel') != 'contradicts':
            continue
        a, b = link['from_id'], link['to_id']
        if a in others:
            others[a].add(b)
        if b in others:
            others[b].add(a)
    resolved = []
    for ident, sides in others.items():
        if not sides:
            continue                          # no recorded counterpart: a person decides
        states = []
        for other in sides:
            row = store.get('brain_facts', other)
            states.append(row['status'] if row else 'retracted')
        if all(st in CLOSED for st in states):
            try:
                store.patch_fact(ident, disputed[ident]['version'], {'status': 'unverified'})
                resolved.append(ident)
            except Exception:
                continue
    return resolved


def _date(iso):
    try:
        value = datetime.fromisoformat(iso)
        return value if value.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _age_h(iso, now_iso):
    before, now = _date(iso), _date(now_iso)
    if before is None or now is None or before > now:
        return None
    return (now - before).total_seconds() / 3600


def queue_report(facts, now_iso):
    ages = {f['id']: _age_h(f.get('recorded_at'), now_iso) for f in facts}
    return dict(disputed_over_24h=sorted(f['id'] for f in facts if f['status'] == 'disputed'
                                       and ages[f['id']] is not None and ages[f['id']] > 24),
                unverified_over_72h=sum(f['status'] == 'unverified' and ages[f['id']] is not None
                                       and ages[f['id']] > 72 for f in facts),
                judge_pending=sum(bool(f.get('judge_pending')) for f in facts),
                age_unknown=sum(age is None for age in ages.values()))


def summary_text(facts, now_iso):
    from bot.brain_graph.views import rank
    counts = Counter(f['status'] for f in facts)
    text = '%d facts (%d verified, %d unverified, %d disputed).' % (
        len(facts), counts['verified'] + counts['verified_partial'], counts['unverified'], counts['disputed'])
    for fact in rank(facts, set(), now_iso):
        text += ' ' + fact['statement']
        if len(text) >= 400:
            break
    return text[:400]


def summaries(store, facts, now_iso):
    entities, sections = store._all('brain_entities'), store._all('brain_sections')
    by_entity, by_section = defaultdict(dict), defaultdict(dict)
    for fact in facts:
        for entity in fact.get('entity_ids') or []:
            by_entity[entity][fact['id']] = fact
    out = dict(entities_patched=0, sections_patched=0)
    for entity in entities:
        fs = by_entity[entity['id']]
        by_section[entity.get('section_id')].update(fs)
        text = summary_text(list(fs.values()), now_iso) if fs else None
        if entity.get('summary') != text:
            store.patch_row('brain_entities', entity['id'], {'summary': text, 'embedding': None})
            out['entities_patched'] += 1
    for section in sections:
        fs = by_section[section['id']]
        text = summary_text(list(fs.values()), now_iso) if fs else None
        if section.get('summary') != text:
            store.patch_row('brain_sections', section['id'], {'summary': text, 'summary_at': now_iso})
            out['sections_patched'] += 1
    out['facts_per_section'] = {section['slug']: len(by_section[section['id']]) for section in sections}
    return out


def _percentile(values, q):
    values = sorted(x for x in values if x is not None)
    return values[round(q * (len(values) - 1))] if values else None


def health_metrics(facts, evidence_out, sweep_out, queue_out, now_iso):
    counts = Counter(f['status'] for f in facts)
    unverified = [_age_h(f.get('recorded_at'), now_iso) for f in facts if f['status'] == 'unverified']
    disputed = [_age_h(f.get('recorded_at'), now_iso) for f in facts if f['status'] == 'disputed']
    by_type = defaultdict(Counter)
    for key, n in (evidence_out.get('checks') or {}).items():
        typ, result = key.split(':', 1)
        by_type[typ][result] += n
    now = _date(now_iso)
    return dict(run_at=now_iso, facts_live=len(facts), by_status=dict(counts),
                share_verified=round((counts['verified'] + counts['verified_partial']) / len(facts), 4) if facts else None,
                unverified_age_h_p50=_percentile(unverified, .5), unverified_age_h_p90=_percentile(unverified, .9),
                disputed=counts['disputed'], disputed_age_h_max=_percentile(disputed, 1),
                past_verify_by=sum(bool(_date(f.get('verify_by')) and _date(f['verify_by']) < now) for f in facts),
                evidence_failure_rate={typ: round(c['broken'] / (c['broken'] + c['ok']), 4)
                                       if c['broken'] + c['ok'] else None for typ, c in by_type.items()},
                evidence_checks=evidence_out.get('checks', {}), flips=evidence_out.get('flips', []),
                recovered_candidates=evidence_out.get('recovered_candidates', []),
                candidates=sweep_out, queue=queue_out)


def doctrine_drift(store, facts, current_rules, *, repo, secret_values):
    """Source+statement identity only. Changed/removed rules need a human supersession judgement."""
    from bot.brain_graph import api
    from bot.brain_graph.import_sources import normalize, split_statement
    def sources(fact):
        return {str(e.get('ref', '')).split(':')[0] for e in fact.get('evidence') or []
                if e.get('type') in ('path', 'doc')} & {'CLAUDE.md', 'AGENTS.md'}
    # Closed doctrine is still a recorded decision; unchanged text must not revive it.
    recorded = store._all('brain_facts', filters=(('author', 'eq', 'operator'), ('kind', 'eq', 'rule')))
    docs = [f for f in recorded if sources(f)]
    have = {(src, normalize(f['statement'])) for f in docs for src in sources(f)}
    current = {}
    for rule in current_rules:
        statement, detail = split_statement(rule['statement'])
        current[(rule['source'], normalize(statement))] = dict(rule, statement=statement, detail=detail)
    out = dict(superseded=0, added=0, doctrine_removed=[], refused=0)
    for f in docs:
        if _live(f) and not any((src, normalize(f['statement'])) in current for src in sources(f)):
            out['doctrine_removed'].append(f['id'])
    for key, rule in current.items():
        if key in have:
            continue
        result = api.write_fact(store, dict(kind='rule', statement=rule['statement'], detail=rule['detail'], author='operator',
            section='brain/doctrine', entities=[dict(type='path', name=rule['source']),
                                              dict(type='doctrine', name=rule['source'])],
            evidence=[dict(type='path', ref=f"{rule['source']}:{rule['line']}")]),
            repo=repo, interactive=False, secret_values=secret_values)
        out['added' if result.get('status') == 'written' else 'refused'] += 1
    return out

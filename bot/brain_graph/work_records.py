"""Explicit durable work records. No transcript inference and no funded-path imports."""
from datetime import datetime, timezone
import json
import re
import subprocess
import uuid

from bot.brain_graph import api, VERIFIERS
from bot.brain_graph.gate import validate_episode_body
from bot.brain_graph.store import BrainRefused, BrainUnavailable

LIVE_COMPLETIONS = ('unverified', 'verified', 'verified_partial')


def _git(repo, *args):
    try:
        result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BrainRefused('git identity unknown') from exc
    if result.returncode:
        raise BrainRefused('git identity or ancestry unverified')
    return result.stdout.strip()


def commit_id(repo, token):
    if not re.fullmatch('[0-9a-f]{7,40}', token or ''):
        raise BrainRefused('expected unambiguous commit SHA')
    return _git(repo, 'rev-parse', '--verify', token+'^{commit}')


def _on_main(repo, sha):
    _git(repo, 'merge-base', '--is-ancestor', sha, 'refs/heads/main')


def _detail(fact):
    try:
        value = json.loads(fact.get('detail') or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _live(fact):
    return bool(fact and fact.get('status') in LIVE_COMPLETIONS and fact.get('valid_to') is None
                and not fact.get('judge_pending'))


def _artifact(store, repo, artifact):
    if not isinstance(artifact, str):
        raise BrainRefused('delivered artifact required')
    if artifact.startswith('episode:'):
        try:
            ident = str(uuid.UUID(artifact[8:]))
        except ValueError as exc:
            raise BrainRefused('invalid episode identity') from exc
        if not store.get('brain_episodes', ident):
            raise BrainRefused('artifact episode missing')
        return 'episode:'+ident, [{'type':'episode','ref':ident}]
    path, sep, token = artifact.rpartition('@')
    if not sep or not path or path.startswith('/') or '..' in path.split('/'):
        raise BrainRefused('committed artifact requires relative path@sha')
    sha = commit_id(repo, token)
    if _git(repo, 'cat-file', '-t', sha+':'+path) != 'blob':
        raise BrainRefused('artifact must name a committed file')
    return path+'@'+sha, [{'type':'path','ref':path}, {'type':'sha','ref':sha[:9]}]


def record_work(store, *, role, statement, validation, repo, merge=None, no_integration=False,
                artifact=None, task_id=None, kind='finding', fetched=True, secret_values=frozenset(),
                outbox=None, judgements=None):
    if role not in VERIFIERS or kind not in ('finding', 'decision') or not (validation or '').strip():
        raise BrainRefused('role, durable kind and validation required')
    if no_integration and kind != 'finding':
        raise BrainRefused('no-integration completion requires a finding')
    if no_integration == bool(merge):
        raise BrainRefused('choose merge SHA or explicit no-integration')
    if task_id:
        task = store.get('brain_facts', task_id)
        if not task or task['kind'] != 'task':
            raise BrainRefused('task missing')
    task_version = (task['version'] if _live(task) else _detail(task).get('completion',{}).get('task_version')) if task_id else None
    if task_id and type(task_version) is not int:
        raise BrainRefused('task completion version unknown')
    entities = [{'type':'actor','name':role}]
    meta = dict(completion_kind='no_integration' if no_integration else 'integration',
                validation=validation.strip(), task_id=task_id, task_version=task_version)
    if no_integration:
        meta['artifact'], evidence = _artifact(store, repo, artifact)
        identity = meta['artifact']
    else:
        sha = commit_id(repo, merge)
        meta['merge_sha'] = identity = sha
        evidence = [{'type':'sha','ref':sha[:9]}]
        entities.append({'type':'commit','name':sha})
    detail = json.dumps({'work':meta}, sort_keys=True)
    draft = api.normalize_operator_order(dict(kind=kind, statement=statement, detail=detail, author=role,
        claim_scope='operational', section='crew', entities=entities, evidence=evidence))
    detail = draft['detail']
    write_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'sniper-work:'+json.dumps([role, identity, task_id, task_version])))
    existing = store.get('brain_facts', write_id)
    if existing:
        if (existing['statement'], existing['kind'], existing.get('detail'), existing['author']) != (statement.strip(), kind, detail, role):
            raise BrainRefused('operation_identity_conflict')
        if not _live(existing):
            raise BrainRefused('completion record is not live')
        return {'status':'written','fact':existing}
    return api.write_fact(store, draft, repo=repo,
        write_id=write_id, fetched=fetched, secret_values=secret_values, outbox=outbox,
        judgements=judgements)


def _qualify(store, fid, task, repo):
    visited = set()
    for _ in range(8):
        if fid in visited:
            raise BrainRefused('completion supersession cycle')
        visited.add(fid)
        fact = store.get('brain_facts', fid)
        if fact and fact.get('status') == 'superseded':
            links = store.rows_page('brain_links', filters=(('to_id','eq',fid),('rel','eq','supersedes')), limit=2)
            if len(links) != 1:
                raise BrainRefused('completion successor unknown')
            fid = links[0]['from_id']
            continue
        if not _live(fact) or fact['kind'] not in ('finding','decision'):
            raise BrainRefused('completion fact missing or not live')
        meta = _detail(fact).get('work', {})
        if meta.get('task_id') != task['id'] or not meta.get('validation'):
            raise BrainRefused('completion task association or validation missing')
        task_version = _detail(task).get('completion',{}).get('task_version') if task['status']=='retracted' else task['version']
        if meta.get('task_version') != task_version:
            raise BrainRefused('task changed since completion was recorded')
        if meta.get('completion_kind') == 'integration':
            sha = commit_id(repo, meta.get('merge_sha'))
            _on_main(repo, sha)
            entity = store.find_entity('commit',sha)
            if not entity or entity['id'] not in fact['entity_ids']:
                raise BrainRefused('completion commit entity missing')
            if not any(e.get('type')=='sha' and e.get('ref') in (sha,sha[:9]) for e in fact['evidence']):
                raise BrainRefused('completion SHA evidence missing')
        elif meta.get('completion_kind') == 'no_integration':
            _artifact(store, repo, meta.get('artifact'))
        else:
            raise BrainRefused('completion kind unknown')
        return fact, meta
    raise BrainRefused('completion successor hop limit')


def complete_work(store, task_id, *, role, completion_fact_id, reason, repo, secret_values=frozenset(), outbox=None):
    if role not in VERIFIERS or not (reason or '').strip() or validate_episode_body(reason, secret_values=set(secret_values)):
        raise BrainRefused('completion role or reason refused')
    task = store.get('brain_facts',task_id)
    if not task or task['kind'] != 'task':
        raise BrainRefused('task missing')
    fact, meta = _qualify(store, completion_fact_id, task, repo)
    link = dict(completion_fact_id=fact['id'],completion_fact_version=fact['version'],
                completion_kind=meta['completion_kind'],task_version=task['version'],role=role,reason=reason)
    if task['status']=='retracted':
        old = _detail(task).get('completion',{})
        if old.get('completion_fact_id') not in (completion_fact_id,fact['id']):
            raise BrainRefused('task already closed by another completion')
        return {'status':'written','task_id':task_id,'completion_fact_id':fact['id']}
    if not _live(task):
        raise BrainRefused('task not live')
    # Capture version before a second read: a concurrent task edit cannot be silently consumed.
    version = task['version']
    fresh = store.get('brain_facts',task_id)
    if fresh['version'] != version:
        raise BrainRefused('task changed during completion')
    fact, meta = _qualify(store,completion_fact_id,task,repo)
    link.update(completion_fact_id=fact['id'],completion_fact_version=fact['version'])
    detail = _detail(task)
    if task.get('detail') and not detail:
        detail['previous_detail'] = task['detail']
    detail['completion'] = link
    encoded = json.dumps(detail,sort_keys=True)
    if len(encoded)>4096:
        raise BrainRefused('closure detail too large')
    store.patch_fact(task_id,version,dict(status='retracted',detail=encoded,
        verified_at=datetime.now(timezone.utc).isoformat(),verify_note=f'completed by {role}: {reason}'))
    try:
        _qualify(store,fact['id'],task,repo)
    except (BrainRefused, BrainUnavailable):
        recovery_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'sniper-reopen:'+task_id+':'+fact['id']))
        result = api.write_fact(store,dict(kind='task',statement=f"reopened: completion fact {fact['id']} lost; {task['statement']}"[:600],
            author=task['author'],section='crew',entities=[{'type':'actor','name':task['author']}],
            evidence=task['evidence'],detail=json.dumps({'reopens':task_id,'lost_completion':fact['id']})),
            repo=repo,write_id=recovery_id,secret_values=secret_values,outbox=outbox,fetched=False)
        return {'status':'reopened' if result['status']=='written' else 'recovery_needed',
                'task_id':result.get('fact',{}).get('id'),'recovery_status':result['status']}
    return {'status':'written','task_id':task_id,'completion_fact_id':fact['id']}


def work_coverage(store, *, repo, base, head):
    base, head = commit_id(repo,base), commit_id(repo,head)
    _on_main(repo,head)
    _git(repo,'merge-base','--is-ancestor',base,head)
    units = _git(repo,'rev-list','--first-parent','--max-count=101',base+'..'+head).splitlines()
    if len(units)>100:
        return dict(complete=False,covered=[],missing=[],unknown=units[:100],reason='unit_limit')
    result = dict(complete=True,covered=[],missing=[],unknown=[],denominator='first_parent_integration_units',total=len(units))
    if not units:
        return result
    hits = store.facts_by_shas(units)
    entities = store.rows_page('brain_entities',filters=(('type','eq','commit'),('name','in','('+','.join(units)+')')),limit=500)
    if len(entities)>=500:
        return dict(result,complete=False,unknown=units)
    entity_ids = {e['name']:e['id'] for e in entities}
    for sha in units:
        info = hits.get(sha)
        if not info or info.get('complete') is not True:
            result['unknown'].append(sha);result['complete']=False
            continue
        matched = False
        for fact in info['facts']:
            meta = _detail(fact).get('work',{})
            if (_live(fact) and fact['kind'] in ('finding','decision') and meta.get('completion_kind')=='integration'
                    and meta.get('merge_sha')==sha and meta.get('validation')
                    and entity_ids.get(sha) in fact['entity_ids']
                    and any(e.get('type')=='sha' and e.get('ref') in (sha,sha[:9]) for e in fact['evidence'])):
                matched = True
        result['covered' if matched else 'missing'].append(sha)
    return result


def closure_audit(store, *, repo, since, until, page_size=500, max_pages=4):
    from collections import Counter
    try:
        start, end = (datetime.fromisoformat(x.replace('Z','+00:00')) for x in (since,until))
        if start.utcoffset() is None or end.utcoffset() is None or not start<end or (end-start).days>31:
            raise ValueError
    except (ValueError,TypeError) as exc:
        raise BrainRefused('closure audit requires aware increasing bounds, at most31days') from exc
    if not 1<=page_size<=500 or not 1<=max_pages<=4:
        raise BrainRefused('closure audit page budget invalid')
    _git(repo, 'rev-parse', '--verify', 'refs/heads/main')
    counts = Counter();after=None;complete=False;seen=0;no_integration=0
    for _ in range(max_pages):
        rows = store.rows_page('brain_facts',filters=(('kind','eq','task'),('status','eq','retracted'),
                     ('verified_at','gte',since),('verified_at','lt',until)),
            select='id,kind,status,statement,detail,verify_note,verified_at,author',after_id=after,limit=page_size)
        for fact in rows:
            seen+=1
            try:
                at = datetime.fromisoformat((fact.get('verified_at') or '').replace('Z','+00:00'))
            except ValueError:
                continue
            if at.utcoffset() is None or not start<=at<end:
                continue
            detail=_detail(fact)
            if detail.get('completion',{}).get('completion_kind')=='no_integration':
                no_integration+=1
            match=re.match(r'^retracted by ([a-z]+) \[(obsolete|misfiled)\]:',fact.get('verify_note') or '')
            if not match:
                continue
            tokens = set(re.findall(r'\b[0-9a-f]{7,40}\b',fact['statement']+' '+(fact.get('verify_note') or '')))
            for token in sorted(tokens):
                try:
                    _on_main(repo,commit_id(repo,token))
                except BrainRefused as exc:
                    if isinstance(exc.__cause__, (OSError, subprocess.TimeoutExpired)):
                        raise
                    continue
                counts[(match[1],at.astimezone(timezone.utc).strftime('%G-W%V'))]+=1
                break
        if len(rows)<page_size:
            complete=True;break
        if after is not None and rows[-1]['id']<=after:
            raise BrainRefused('closure audit paging did not advance')
        after=rows[-1]['id']
    return dict(complete=complete,scanned=seen,since=since,until=until,no_integration_completions=no_integration,
        suspicious=[dict(role=role,week=week,count=n) for (role,week),n in sorted(counts.items())],
        scope='bounded_retracted_task_scan; incomplete counts are lower bounds')


def check_fact_inventory(store, role, inventory):
    from .handover_inventory import is_open, LEGACY_ORDER_REFERENCES
    digest_key = 'brain_content_digest' if isinstance(inventory,dict) and inventory.get('scope')=='brain_only' else 'content_digest'
    if (not isinstance(inventory,dict) or inventory.get('complete') is not True
            or inventory.get('pagination_complete') is not True
            or not re.fullmatch('[0-9a-f]{64}',str(inventory.get(digest_key,'')))):
        raise BrainRefused('fact inventory incomplete')
    actor=store.find_entity('actor',role)
    current=store.find_facts([actor['id']],kinds=['task']) if actor else []
    current={f['id']:f for f in current if is_open(f)}
    tasks=inventory.get('tasks');orders=inventory.get('operator_order_ids')
    if not isinstance(tasks,list) or not isinstance(orders,list):
        raise BrainRefused('task/order inventory missing')
    expected={}
    for item in tasks:
        if (not isinstance(item,dict) or item.get('owner')!=role or type(item.get('version')) is not int
                or not isinstance(item.get('id'),str) or item['id'] in expected):
            raise BrainRefused('task inventory identity invalid')
        expected[item['id']]=item['version']
    if expected!={fid:f['version'] for fid,f in current.items()}:
        raise BrainRefused('open task facts differ from inventory')
    operator=store.find_entity('actor','operator')
    if any(not isinstance(fid,str) for fid in orders) or len(set(orders))!=len(orders):
        raise BrainRefused('order inventory invalid')
    lines=[]
    for fid in orders:
        fact=store.get('brain_facts',fid)
        if not is_open(fact) or fact['kind'] not in ('decision','settled') or not operator:
            raise BrainRefused('operator order fact missing or invalid')
        if operator['id'] not in fact['entity_ids']:
            parents=[p for p,t in LEGACY_ORDER_REFERENCES if t==fid and p in orders]
            linked=False
            for parent in parents:
                pointer=store.get('brain_facts',parent)
                if (is_open(pointer) and pointer['kind'] in ('decision','settled') and operator['id'] in pointer['entity_ids']):
                    linked=True;break
            if not linked:raise BrainRefused('operator order target lacks pinned live pointer')
        lines.append(fact)
    return dict(task_ids=sorted(current),order_facts=lines,**{digest_key:inventory[digest_key]})

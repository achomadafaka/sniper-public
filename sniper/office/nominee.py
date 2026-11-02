"""Bounded local nominee resolution. Inputs are crew-owned, read-only pinned bytes."""
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import time

from . import memory as M, memory_store as MS, store as S
from .grader import validate_calibration
from .costs import RULE_ID
from .epoch_transition import tool_hash
from .readiness import verdict as V
from .pods import validate_rule

REQUIRED = frozenset({'memory_snapshot_id','learning_rule','prompt_hashes','model_ids','pods_rule_sha256',
    'cost_rule_id','metric_registry_hash','calibration_proof_id','delta0','delta1','paper_drawdown',
    'clip_usd','min_epoch_days','l_null_file_hash','exec_bound_evidence_id','host_guard_manifest_hash',
    'stopping_rule','n_plan','alpha_scale','block_days','sigma','day_method','tool_contract_hash'})
FILES = {'pods_rule':'pods.json', 'book_policy':'book_policy.json', 'cost_rule':'cost_allocation_rule.json',
         'metric_registry':'metric_registry.json', 'l_null':'l_null.json', 'host_guard':'host_guard.json'}


class Invalid(ValueError):
    pass


def _finite(v):
    return type(v) in (int, float) and math.isfinite(v)


def _check_time(deadline):
    if time.monotonic() > deadline:
        raise Invalid('nominee_resolution_budget')


def _read(root, ref, deadline, *, authority=None):
    _check_time(deadline)
    if not isinstance(ref, dict) or set(ref) != {'path','sha256'}:
        raise Invalid('unresolved_file')
    rel = PurePosixPath(ref['path'])
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise Invalid('nominee_path_denied')
    fd = os.dup(root) if type(root) is int else os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel.parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        file_fd = os.open(rel.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222:
                raise Invalid('nominee_file_not_readonly')
            if authority is not None:
                from . import launcher_policy as LP
                if str(rel) not in authority.probes or not LP.readonly_mount(stream.fileno()):
                    raise Invalid('nominee_file_readonly_authority_unknown')
                uid = authority.policy['office_uid']
                if info.st_uid == uid:
                    if uid == os.geteuid():
                        safe = not os.access('/proc/self/fd/'+str(fd), os.W_OK, effective_ids=True)
                    else:
                        safe = authority.probes.get(str(rel.parent)) is False
                    if not safe:
                        raise Invalid('office_can_modify_owned_configuration')
            raw = stream.read(262145)
        if len(raw) > 262144 or hashlib.sha256(raw).hexdigest() != ref['sha256']:
            raise Invalid('nominee_file_changed')
        _check_time(deadline)
        return raw
    finally:
        os.close(fd)


def resolve(conn, nominee, refs, *, root, k, opened_at, deadline, authority):
    """Resolve once per file; compare bytes and live roster/snapshot, then return canonical copies."""
    try:
        if not isinstance(nominee, dict) or not REQUIRED <= set(nominee):
            raise Invalid('nominee_missing_key')
        if any(nominee[key] in (None, '', 'unmeasured') for key in REQUIRED):
            raise Invalid('nominee_unmeasured')
        if not isinstance(refs, dict) or len(refs) > 40:
            raise Invalid('nominee_refs_invalid')
        root = Path(root).resolve(strict=True)
        raw = {key: _read(authority.root_fd, ref, deadline, authority=authority) for key, ref in refs.items()}
        for key, filename in FILES.items():
            if refs.get(key, {}).get('path') != filename:
                raise Invalid('nominee_file_identity')
        docs = {key: json.loads(value) for key, value in raw.items() if not key.startswith('prompt:')}
        checks = {'pods_rule_sha256':'pods_rule','metric_registry_hash':'metric_registry',
                  'l_null_file_hash':'l_null','host_guard_manifest_hash':'host_guard'}
        if any(nominee[field] != refs[key]['sha256'] for field,key in checks.items()):
            raise Invalid('nominee_hash_mismatch')
        if nominee['host_guard_manifest_hash'] != authority.policy['approved_full_guard_sha256']:
            raise Invalid('full_guard_not_launcher_approved')
        learning = nominee['learning_rule']
        if learning != {'id': docs['learning_rule']['id'], 'sha256': refs['learning_rule']['sha256']}:
            raise Invalid('learning_rule_mismatch')
        if nominee['cost_rule_id'] != RULE_ID or hashlib.sha256(S.canonical_json(docs['cost_rule'])).hexdigest() != RULE_ID:
            raise Invalid('cost_rule_mismatch')
        if nominee['tool_contract_hash'] != tool_hash():
            raise Invalid('tool_contract_mismatch')
        guard = docs['host_guard']
        if guard.get('mode') != 'full' or guard.get('provisional') is not False or guard.get('evaluation_allowed') is not True:
            raise Invalid('pilot_discovery_only')
        active = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM agents WHERE json_extract(payload_json,'$.state')='active' LIMIT 25")]
        if not 1 <= len(active) <= 24:
            raise Invalid('nominee_roster_invalid')
        prompts = {a['agent_id']: a['prompt_hash'] for a in active}
        models = {a['agent_id']: {key:a[key] for key in ('model','provider','family')} for a in active}
        if nominee['prompt_hashes'] != prompts or nominee['model_ids'] != models:
            raise Invalid('nominee_roster_changed')
        if any(refs['prompt:'+aid]['sha256'] != value for aid,value in prompts.items()):
            raise Invalid('nominee_prompt_changed')
        mem = MS.OfficeMemory(conn)
        size = conn.execute('SELECT length(payload_json) FROM memory_snapshots WHERE snapshot_id=?', (nominee['memory_snapshot_id'],)).fetchone()
        if size is None or size[0] > 262144:
            raise Invalid('nominee_snapshot_unbounded')
        snap = mem.get_snapshot(nominee['memory_snapshot_id'])
        if snap is None or snap['cut'] >= opened_at or M._snapshot_id(snap) != nominee['memory_snapshot_id']:
            raise Invalid('nominee_snapshot_invalid')
        if len(snap['members']) + len(snap['supersessions']) > 1024:
            raise Invalid('nominee_snapshot_unbounded')
        M.retrieve(mem, '', decision_cut=snap['cut'], candidate={}, viewer_role='System', k=0,
                   snapshot=snap, trusted_snapshot_id=nominee['memory_snapshot_id'])
        if (any(not _finite(nominee[key]) or nominee[key] <= 0 for key in ('delta0','delta1','paper_drawdown','clip_usd','sigma'))
                or nominee['delta1'] <= nominee['delta0']
                or type(nominee['min_epoch_days']) is not int or nominee['min_epoch_days'] < 30
                or type(nominee['n_plan']) is not int or nominee['n_plan'] < 1
                or nominee['stopping_rule'] != 'single-look-at-n-plan-and-min-days'):
            raise Invalid('nominee_design_invalid')
        params = dict(nominee, k=k, days=nominee['min_epoch_days'], alpha_k=V.alpha_k(k), draws=V.draws_for(V.alpha_k(k)))
        calibration = docs['calibration']
        if calibration.get('proof_id') != nominee['calibration_proof_id']:
            raise Invalid('calibration_identity_mismatch')
        validate_calibration(calibration, params)
        economic = docs['metric_registry']['economics']
        if economic['clip'] != nominee['clip_usd'] or not _finite(economic['bound_usd_per_fill']) or economic['bound_usd_per_fill'] < 0:
            raise Invalid('nominee_execution_bound_unmeasured')
        bound = docs['execution_bound']
        if (bound.get('id') != nominee['exec_bound_evidence_id'] or economic['exec_bound_evidence_id'] != bound.get('id')
                or bound.get('bound_usd_per_fill') != economic['bound_usd_per_fill']
                or any(bound.get(key) != economic.get(key) for key in ('venue','clip_band','latency_regime'))
                or not _finite(bound.get('created_at')) or bound['created_at'] > opened_at
                or type(bound.get('sample_n')) is not int or bound['sample_n'] < 1 or not bound.get('source_refs')):
            raise Invalid('nominee_execution_bound_unresolved')
        pods = validate_rule(docs['pods_rule'])
        book = docs['book_policy'].get('policy') if isinstance(docs['book_policy'], dict) else None
        if (not isinstance(book, dict) or book.get('clip_usd') != nominee['clip_usd']
                or type(book.get('max_concurrent')) is not int or not 1 <= book['max_concurrent'] <= pods['max_concurrent_total']
                or not _finite(book.get('daily_loss_usd')) or not 0 < book['daily_loss_usd'] <= pods['daily_loss_usd_total']):
            raise Invalid('nominee_book_policy_exceeds_pods')     # Gamma note 2: the book caps fit the pod totals
        shifts = docs['l_null'].get('shifts')
        if not isinstance(shifts, list) or not shifts or any(not _finite(s.get('l_null_s')) or s['l_null_s'] < 0 for s in shifts):
            raise Invalid('nominee_latency_unmeasured')
        _check_time(deadline)
        return json.loads(S.canonical_json(nominee)), json.loads(S.canonical_json(refs))
    except (OSError, KeyError, TypeError, ValueError, M.Refused) as exc:
        raise Invalid(str(exc)) from None

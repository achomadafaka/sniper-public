"""Office Task 8: the deterministic grader (pure scoring; the store wiring is added with Task 1). It grades a FROZEN claim (decision + stated probability of the registry event + horizon) against an outcome
produced by capture.py and the pinned fill model. ENTER, WAIT, SKIP, veto and uncovered predictions are all graded on the same event, so skill is not measured on the winners only.
y = the registry event (net trading P&L at the fixed clip, after the cost bound B, is positive at the frozen horizon). A probability outside [0, 1] or missing is REFUSED, never clipped.
Unknown is typed: path_gap / no_pool / source_off / no_swaps => censored with the reason, pending => pending; neither is a loss, a zero or part of the skill denominator.
The metric registry is crew-owned; the grader loads it only against a PINNED sha256, so the Office cannot move the goalposts by editing the file."""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math

ECONOMIC_CONTRACT = 'office-economics-horizon-v2'


class RegistryMismatch(Exception):
    pass


class BadClaim(ValueError):
    pass


def load_registry(path, expected_sha256):
    raw = open(path, "rb").read()
    got = hashlib.sha256(raw).hexdigest()
    if got != expected_sha256:
        raise RegistryMismatch("metric registry sha256 %s != pinned %s" % (got, expected_sha256))
    return json.loads(raw)


def grade(claim, outcome):
    p = claim.get("p_win")
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0.0 <= p <= 1.0:
        raise BadClaim("p_win must be a finite number in [0, 1], got %r" % (p,))
    state = outcome.get("state")
    if state == "pending":
        return {"state": "pending"}
    if state != "observed" or outcome.get("net_pnl_usd") is None:
        return {"state": "censored", "reason": outcome.get("reason") or state}
    y = 1 if outcome["net_pnl_usd"] > 0 else 0
    return {"state": "graded", "y": y, "p": float(p), "brier": (float(p) - y) ** 2}


def skill(grades, *, min_graded):
    """Brier skill score against climatology (the base rate), over graded rows only; None (unknown, not 0) below min_graded."""
    g = [x for x in grades if x.get("state") == "graded"]
    if len(g) < min_graded:
        return None
    base = sum(x["y"] for x in g) / len(g)
    ref = base * (1 - base)
    mean = sum(x["brier"] for x in g) / len(g)
    return {"n": len(g), "base_rate": base, "brier": mean, "bss": None if ref == 0 else 1 - mean / ref}


def _finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _cut_proven(receipt, horizon_at, look_at):
    if not isinstance(receipt, dict):
        return False
    proof = receipt.get('execution_receipt_hash')
    return (isinstance(proof, str) and len(proof) == 64
            and all(c in '0123456789abcdef' for c in proof)
            and all(_finite(receipt.get(k)) for k in ('horizon_at', 'as_of_cut', 'known_at'))
            and receipt['horizon_at'] == receipt['as_of_cut'] == horizon_at
            and horizon_at <= receipt['known_at'] <= look_at)


def economic_outcome(execution, *, bound_usd_per_fill, horizon_at, look_at):
    """Resolver reconstructs immutable state at the trusted horizon, never the current order.

    Receipt hashes must be verified by the host resolver. These checks bind its
    result to the frozen cut; a hash-shaped string alone is not authenticity.
    """
    if not _finite(bound_usd_per_fill) or bound_usd_per_fill < 0:
        raise ValueError('invalid_execution_bound')
    if not _finite(horizon_at) or not _finite(look_at) or not 0 <= horizon_at <= look_at:
        raise ValueError('invalid_horizon_contract')
    unknown = dict(entered=None, y=None, y_censored=True, partial=None, reason='entry_telemetry_unknown')
    if not _cut_proven(execution, horizon_at, look_at):
        return unknown
    if execution.get('state') == 'no_entry':
        if (execution.get('entry_filled_at') is not None or execution.get('closed_at') is not None
                or ('fill_count' in execution and (type(execution['fill_count']) is not int or execution['fill_count'] != 0))
                or ('net_cashflow_usd' in execution and (not _finite(execution['net_cashflow_usd']) or execution['net_cashflow_usd'] != 0))
                or (execution.get('partial') is not None and execution['partial'] is not False)):
            return unknown
        return dict(entered=False, y=0., y_censored=False, partial=False, reason=None)
    entered_at = execution.get('entry_filled_at')
    if not _finite(entered_at) or not 0 <= entered_at <= horizon_at:
        return unknown
    known = dict(unknown, entered=True, reason=execution.get('reason') or 'outcome_unknown')
    if execution.get('state') != 'closed':
        return known
    closed_at = execution.get('closed_at')
    if not _finite(closed_at) or not entered_at <= closed_at <= horizon_at:
        return dict(known, reason='close_outside_horizon')
    net, count = execution.get('net_cashflow_usd'), execution.get('fill_count')
    if not _finite(net) or type(count) is not int or count < 2 or type(execution.get('partial')) is not bool:
        return known
    scored = net - count * bound_usd_per_fill
    if not _finite(scored):
        return known
    return dict(entered=True, y=scored, y_censored=False,
                partial=execution['partial'], reason=None)


def grade_epoch(office, evaluation, epoch_id, *, execution_lookup, null_lookup, bound_usd_per_fill, look_id, registry_path):
    """Private grading against the epoch-pinned registry and registered corrected-UTC look.

    Both trusted resolvers must verify immutable receipt identity and reconstruct
    state at first_eligible_at + horizon_s. Their horizon_at/as_of_cut must equal
    that cut, known_at must lie in [horizon, look], and a closed execution needs
    entry_filled_at <= closed_at <= horizon. Later mutable order rows are not proof.
    """
    from datetime import datetime, timezone
    from sniper.office.store import append, canonical_json, write_transaction
    if not isinstance(look_id, str) or not look_id:
        raise ValueError('look_id_required')
    purpose = evaluation.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()
    if purpose is None or purpose[0] != 'evaluation':
        raise ValueError('private_evaluation_store_required')
    epoch_row = office.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
    epoch = json.loads(epoch_row[0]) if epoch_row else {}
    look_at = epoch.get('registered_look_at')
    if epoch.get('registered_look_id') != look_id or not _finite(look_at) or look_at < 0:
        raise ValueError('unregistered_look')
    registry = load_registry(registry_path, epoch.get('metric_registry_hash'))
    horizon_s = registry.get('economics', {}).get('horizon_s')
    if not _finite(horizon_s) or horizon_s <= 0:
        raise ValueError('horizon_unmeasured')
    rows = []
    for source in office.execute('SELECT seq,payload_json,row_hash FROM opportunity_log WHERE epoch_id=? ORDER BY seq', (epoch_id,)):
        seq, payload, source_hash = source
        op = dict(json.loads(payload), candidate_seq=seq)
        eligible_at = op.get('first_eligible_at')
        if not _finite(eligible_at) or eligible_at < 0:
            raise ValueError('opportunity_clock_unknown')
        horizon_at = eligible_at + horizon_s
        if not _finite(horizon_at) or horizon_at > look_at:
            raise ValueError('horizon_not_mature_at_look')
        decision_row = office.execute('SELECT payload_json FROM decisions WHERE candidate_seq=? ORDER BY seq DESC LIMIT 1', (seq,)).fetchone()
        decision = json.loads(decision_row[0]) if decision_row else None
        execution = execution_lookup(op, decision)
        null = null_lookup(op)
        result = economic_outcome(execution, bound_usd_per_fill=bound_usd_per_fill, horizon_at=horizon_at, look_at=look_at)
        n = null.get('n') if isinstance(null, dict) else None
        null_proof = null.get('execution_receipt_hash') if isinstance(null, dict) else None
        # Capacity is an explicit proven no-entry state only for the primary null.
        null_execution = dict(null, state='no_entry') if isinstance(null, dict) and null.get('state') == 'capacity_refused' else null
        null_result = economic_outcome(null_execution, bound_usd_per_fill=bound_usd_per_fill,
                                       horizon_at=horizon_at, look_at=look_at)
        n_censored = (not _finite(n) or null_result['y_censored'] or n != null_result['y']
                      or null.get('bound_usd_per_fill') != bound_usd_per_fill)
        inputs = dict(opportunity_hash=source_hash, decision=decision, execution=execution,
                      null=null, bound_usd_per_fill=bound_usd_per_fill, look_id=look_id,
                      horizon_at=horizon_at, look_at=look_at, metric_registry_hash=epoch['metric_registry_hash'],
                      grading_contract=ECONOMIC_CONTRACT)
        digest = hashlib.sha256(canonical_json(inputs)).hexdigest()
        row = dict(result, epoch_id=epoch_id, candidate_seq=seq, kind='coin_economics_v1', look_id=look_id,
                   input_hash=digest, grading_contract=ECONOMIC_CONTRACT, horizon_at=horizon_at, look_at=look_at,
                   metric_registry_hash=epoch['metric_registry_hash'],
                   bound_usd_per_fill=bound_usd_per_fill, opportunity_hash=source_hash, coin_id=op['network_id'] + ':' + op['mint'],
                   day=datetime.fromtimestamp(op['first_eligible_at'], timezone.utc).date().isoformat(),
                   shift_id=op['shift_id'], decision_id=decision.get('decision_id') if decision else None,
                   actor_id=decision.get('actor_id') if decision else None,
                   n=None if n_censored else n, n_censored=n_censored, null_receipt_hash=null_proof)
        with write_transaction(evaluation):
            previous = evaluation.execute("SELECT payload_json FROM grades WHERE candidate_seq=? AND kind='coin_economics_v1' AND json_extract(payload_json,'$.look_id')=? ORDER BY seq DESC LIMIT 1", (seq, look_id)).fetchone()
            if previous:
                prior = json.loads(previous[0])
                if prior.get('grading_contract') != ECONOMIC_CONTRACT or prior['input_hash'] != digest:
                    raise ValueError('frozen_look_inputs_changed')
                row = prior
            else:
                append(evaluation, 'grades', row)
        rows.append(row)
    return rows


def read_proof(root, ref):
    """Resolve a frozen id and byte hash inside the evaluator's approved proof root."""
    from pathlib import Path
    root = Path(root).resolve()
    if not isinstance(ref, dict) or any(not isinstance(ref.get(k), str) or not ref[k] or ref[k] == 'unmeasured' for k in ('id', 'path', 'sha256')):
        raise ValueError('unresolved_proof')
    path = (root / ref['path']).resolve()
    if not path.is_relative_to(root):
        raise ValueError('proof_path_escape')
    with path.open('rb') as stream:
        raw = stream.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('proof_too_large')
    if hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('proof_hash_mismatch')
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get('proof_id', doc.get('id')) != ref['id']:
        raise ValueError('proof_identity_mismatch')
    return doc


def validate_calibration(doc, params):
    """Recheck the exact six-generator design, not a caller-supplied supported flag."""
    from sniper.office.store import canonical_json
    if doc.get('proof_id') == '1bd1d0507535effba801103929d5f6c0f4fc84aee43f3858ca7f3bfc431433c8':
        raise ValueError('withdrawn_calibration_proof')
    names = ('iid', 'heavy_tail', 'day_cluster', 'serial', 'adaptive_entry', 'pods_shifts')
    generators = doc.get('generators')
    if not isinstance(generators, list) or len(generators) != len(names) or sorted(g.get('name', '') for g in generators if isinstance(g, dict)) != sorted(names) or any(g.get('supported') is not True for g in generators):
        raise ValueError('calibration_generators')
    design = doc.get('params', {})
    matches = {'n_coins': 'n_plan', 'days': 'days', 'block_days': 'block_days', 'alpha_scale': 'alpha_scale', 'delta0': 'delta0', 'delta1': 'delta1', 'day_method': 'day_method', 'sigma': 'sigma', 'draws': 'draws', 'alpha_k': 'alpha_k'}
    if doc.get('schema') != 'office-calibration-proof-v1' or type(doc.get('k')) is not int or doc['k'] != params.get('k') or doc.get('supported_for_k') is not True or any(design.get(key) is None or design.get(key) != params.get(other) for key, other in matches.items()):
        raise ValueError('calibration_design_mismatch')
    from sniper.office.readiness.verdict import alpha_k, draws_for
    k = params.get('k')
    if type(k) is not int or not 1 <= k <= 10 or params.get('days', 0) < 30 or params.get('alpha_k') != alpha_k(k) or params.get('draws') != draws_for(alpha_k(k)) or params.get('draws_cap') is not None:
        raise ValueError('calibration_production_design_required')
    body = {k: v for k, v in doc.items() if k != 'proof_id'}
    if hashlib.sha256(canonical_json(body)).hexdigest() != doc.get('proof_id'):
        raise ValueError('calibration_content_hash_mismatch')


def export_epoch(office, evaluation, epoch_id, *, manifest_path, manifest_sha256, config_root, audit_snapshot, clock=None,
                 authenticate_snapshot=None, runtime_dir=None, launcher_policy=None, probe_write=None):
    """Verify the live nominee before snapshotting. A changed nominee durably breaks the trial.

    Evaluated export needs an owned Office control transaction and trusted corrected
    clock; it never commits or rolls back a caller-owned transaction to record BROKEN.
    """
    from sniper.office.store import canonical_json
    from sniper.office import roster
    epoch_row = office.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
    epoch = json.loads(epoch_row[0]) if epoch_row else {}
    if epoch.get('evaluation_manifest_sha256') == manifest_sha256:
        if not callable(clock):
            raise ValueError('export_clock_required')
        roster.verify_nominee(office, epoch_id=epoch_id, config_root=config_root, clock=clock, launcher_policy=launcher_policy, probe_write=probe_write)
    frozen_audit = json.loads(canonical_json(audit_snapshot))
    try:
        with ExitStack() as stack:
            for conn, expected in ((office, 'office'), (evaluation, 'evaluation')):
                stack.enter_context(_read_snapshot(conn))
                purpose = conn.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()
                if purpose is None or purpose[0] != expected:
                    raise ValueError('export_store_purpose_mismatch')
            result = _export_epoch(office, evaluation, epoch_id, manifest_path=manifest_path,
                                  manifest_sha256=manifest_sha256, config_root=config_root,
                                  audit_snapshot=frozen_audit, authenticate_snapshot=authenticate_snapshot, runtime_dir=runtime_dir)
    except Exception:
        if epoch.get('evaluation_manifest_sha256') == manifest_sha256:
            roster.verify_nominee(office, epoch_id=epoch_id, config_root=config_root, clock=clock, launcher_policy=launcher_policy, probe_write=probe_write)
        raise
    if epoch.get('evaluation_manifest_sha256') == manifest_sha256:
        roster.verify_nominee(office, epoch_id=epoch_id, config_root=config_root, clock=clock, launcher_policy=launcher_policy, probe_write=probe_write)
    return result


@contextmanager
def _read_snapshot(conn):
    own = not conn.in_transaction
    if own:
        conn.execute('BEGIN')
    try:
        yield
    finally:
        if own:
            conn.rollback()


def _export_epoch(office, evaluation, epoch_id, *, manifest_path, manifest_sha256, config_root, audit_snapshot,
                  authenticate_snapshot, runtime_dir):
    """Evaluator-only export from a crew-frozen manifest; no agent-supplied evidence or cost inputs."""
    from pathlib import Path
    from sniper.office.costs import RULE_ID, epoch_cost
    from sniper.office.store import canonical_json
    epoch_row = office.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
    epoch = json.loads(epoch_row[0]) if epoch_row else {}
    if epoch.get('evaluation_manifest_sha256') != manifest_sha256:
        raise ValueError('epoch_manifest_not_frozen')
    manifest = load_registry(manifest_path, manifest_sha256)
    if manifest.get('epoch_id') != epoch_id or epoch.get('evaluation_broken') is not False or epoch.get('status') == 'broken' or epoch.get('opened_at') != manifest.get('opened_at'):
        raise ValueError('epoch_not_evaluable')
    if epoch.get('registered_look_id') != manifest.get('look_id') or epoch.get('registered_look_at') != manifest.get('look_at'):
        raise ValueError('equity_look_not_frozen')
    root = Path(config_root)
    registry = load_registry(root / 'metric_registry.json', manifest['metric_registry_hash'])
    economic = registry.get('economics', {})
    if any(economic.get(k) in (None, 'unmeasured') for k in ('clip', 'bound_usd_per_fill', 'loss_bound_components', 'horizon_s', 'venue', 'clip_band', 'latency_regime')):
        raise ValueError('economics_unmeasured')
    params = manifest['params']
    nominee = epoch['nominee']
    if (nominee['metric_registry_hash'] != manifest['metric_registry_hash']
            or nominee['l_null_file_hash'] != manifest['l_null_hash']
            or nominee['calibration_proof_id'] != manifest['calibration']['id']
            or nominee['exec_bound_evidence_id'] != manifest['execution_bound']['id']
            or nominee['cost_rule_id'] != manifest['cost_rule_id']
            or any(nominee[key] != params[key] for key in ('delta0','delta1','n_plan','alpha_scale','block_days','day_method'))
            or nominee['min_epoch_days'] != params['days'] or nominee['clip_usd'] != params['clip']):
        raise ValueError('manifest_nominee_mismatch')
    if not isinstance(params, dict) or any(not _finite(params.get(k)) for k in ('clip', 'loss_bound', 'delta0', 'delta1', 'alpha_scale', 'sigma')) or any(type(params.get(k)) is not int or params[k] < 1 for k in ('k', 'n_plan', 'days', 'block_days', 'draws')) or params['clip'] <= 0 or params['delta1'] <= params['delta0'] or not 0 < params['alpha_scale'] <= 1:
        raise ValueError('invalid_epoch_parameters')
    if params.get('n_upper') is not None:
        raise ValueError('null_upper_bound_unproven')
    if economic.get('mark_max_age_s') != 60 or economic['clip'] != params['clip']:
        raise ValueError('metric_registry_mismatch')
    if not _finite(economic['bound_usd_per_fill']) or economic['bound_usd_per_fill'] < 0 or not _finite(economic['horizon_s']) or economic['horizon_s'] <= 0:
        raise ValueError('invalid_economic_registry')
    components = economic['loss_bound_components']
    if not isinstance(components, dict) or set(components) != {'clip', 'B_entry', 'B_exit', 'max_network_rent'} or any(not _finite(v) or v < 0 for v in components.values()) or components['clip'] != params['clip'] or sum(components.values()) != params['loss_bound'] or components['B_entry'] != economic['bound_usd_per_fill'] or components['B_exit'] != economic['bound_usd_per_fill']:
        raise ValueError('loss_bound_mismatch')
    rule = load_registry(root / 'cost_allocation_rule.json', manifest['cost_rule_hash'])
    if hashlib.sha256(canonical_json(rule)).hexdigest() != RULE_ID or manifest.get('cost_rule_id') != RULE_ID:
        raise ValueError('retired_cost_rule')
    cost = epoch_cost(opened_at=manifest['opened_at'], ended_at=manifest['ended_at'], look_at=manifest['look_at'])
    if cost['state'] != 'known':
        raise ValueError('epoch_still_open')
    if len(cost['days']) != params.get('days'):
        raise ValueError('epoch_day_design_mismatch')
    if not isinstance(audit_snapshot, dict) or audit_snapshot.get('state') != 'clean' or audit_snapshot.get('epoch_id') != epoch_id or not _finite(audit_snapshot.get('at')) or audit_snapshot['at'] < manifest['look_at']:
        raise ValueError('auditor_not_clean')
    for name in ('office', 'evaluation', 'reconciliation', 'null_reconciliation', 'safety'):
        if audit_snapshot.get('checks', {}).get(name, {}).get('state') != 'clean':
            raise ValueError('auditor_not_clean')
    for name, conn in (('office', office), ('evaluation', evaluation)):
        heads = audit_snapshot['checks'][name].get('heads', {})
        if not heads:
            raise ValueError('auditor_heads_missing')
        from sniper.office.store import APPEND_ONLY_TABLES, PRIVATE_TABLES
        expected = PRIVATE_TABLES if name == 'evaluation' else APPEND_ONLY_TABLES - PRIVATE_TABLES
        if set(heads) != set(expected):
            raise ValueError('auditor_heads_incomplete')
        for table in expected:
            cut = heads[table]
            if not isinstance(cut, list) or len(cut) != 2 or type(cut[0]) is not int or cut[0] < 0 or not isinstance(cut[1], str):
                raise ValueError('invalid_auditor_head')
            row = conn.execute('SELECT row_hash FROM '+table+' WHERE seq=?', (cut[0],)).fetchone() if cut[0] else None
            if (cut[0] and (row is None or row[0] != cut[1])) or (cut[0] == 0 and cut[1] != ''):
                raise ValueError('auditor_heads_changed')
            from sniper.office.store import verify_chain
            if not verify_chain(conn, table)[0]:
                raise ValueError('chain_changed_after_audit')
    calibration = read_proof(root / 'proofs', manifest['calibration'])
    validate_calibration(calibration, params)
    if not _finite(calibration.get('created_at')) or calibration['created_at'] > manifest['opened_at']:
        raise ValueError('calibration_not_prior')
    bound = read_proof(root / 'proofs', manifest['execution_bound'])
    if any(bound.get(k) != economic.get(k) for k in ('venue', 'clip_band', 'latency_regime', 'bound_usd_per_fill')) or bound.get('id') != economic.get('exec_bound_evidence_id') or not _finite(bound.get('bound_usd_per_fill')) or bound['bound_usd_per_fill'] < 0 or type(bound.get('sample_n')) is not int or bound['sample_n'] < 1 or not bound.get('source_refs') or not bound.get('method'):
        raise ValueError('execution_bound_mismatch')
    if not _finite(bound.get('created_at')) or bound['created_at'] > manifest['opened_at']:
        raise ValueError('execution_bound_not_prior')
    delays = load_registry(root / 'l_null.json', manifest['l_null_hash'])
    from sniper.office.null_arm import submission_time
    if delays.get('epoch_id') != epoch_id or not isinstance(delays.get('shifts'), list) or not delays['shifts']:
        raise ValueError('null_latency_unmeasured')
    from sniper.office import shifts as shift_rules
    shift_config = shift_rules.load(root / 'shifts.json', manifest['shifts_hash'])
    configured = {s['id']: s for s in shift_config['shifts']}
    shifts, attempts = set(), set()
    for shift in delays['shifts']:
        submission_time(0., shift.get('l_null_s'))
        if not shift.get('shift_id') or shift['shift_id'] in shifts or type(shift.get('sample_n')) is not int or shift['sample_n'] < 1 or not shift.get('evidence_ref'):
            raise ValueError('null_latency_unmeasured')
        sid = shift['shift_id']
        if sid not in configured or shift.get('utc_hours') != [configured[sid]['start_hour'], configured[sid]['end_hour']]:
            raise ValueError('null_shift_schedule_mismatch')
        evidence = read_proof(root / 'proofs', manifest.get('l_null_evidence_refs', {}).get(shift['evidence_ref']))
        ids = validate_null_evidence(evidence, shift, opened_at=manifest['opened_at'], epoch_id=epoch_id, shift_config=shift_config)
        if attempts.intersection(ids):
            raise ValueError('duplicate_null_latency_attempt')
        attempts.update(ids)
        shifts.add(sid)
    if shifts != set(configured):
        raise ValueError('null_shift_evidence_incomplete')
    public_heads = audit_snapshot['checks']['office']['heads']
    private_heads = audit_snapshot['checks']['evaluation']['heads']
    gaps = [json.loads(r[0]) for r in office.execute('SELECT payload_json FROM opportunity_gaps WHERE epoch_id=? AND seq<=? ORDER BY seq', (epoch_id, public_heads['opportunity_gaps'][0]))]
    if any(g.get('state') in ('off', 'unknown') for g in gaps):
        raise ValueError('opportunity_coverage_unknown')
    expected_sources = manifest.get('eligible_sources')
    if not isinstance(expected_sources, list) or not expected_sources or any(not isinstance(s, str) or not s for s in expected_sources) or len(set(expected_sources)) != len(expected_sources):
        raise ValueError('eligible_sources_not_frozen')
    from sniper.office.capture import coverage_proof
    coverage_heads = audit_snapshot['checks']['office']['heads']
    for source in expected_sources:
        reach = manifest['opened_at']
        for payload, in office.execute('SELECT payload_json FROM opportunity_coverage WHERE epoch_id=? AND source=? AND seq<=? ORDER BY from_at,seq', (epoch_id, source, coverage_heads['opportunity_coverage'][0])):
            interval = json.loads(payload)
            lo, hi = interval.get('from_at'), interval.get('to_at')
            if interval.get('state') != 'observed' or not _finite(lo) or not _finite(hi) or hi <= lo:
                raise ValueError('opportunity_coverage_unknown')
            proof = coverage_proof(office, source=source, from_at=lo, to_at=hi, proof_hash=interval.get('proof_hash'), max_seq=coverage_heads['receipts'][0])
            if proof['known_at'] > manifest['look_at']:
                raise ValueError('opportunity_coverage_unknown')
            if hi <= reach:
                continue
            if lo > reach:
                break
            reach = hi
        if reach < manifest['look_at']:
            raise ValueError('opportunity_coverage_unknown')
    from sniper.office.auditor import COUNTERS
    counters = audit_snapshot['checks']['safety'].get('counters', {})
    if set(counters) != set(COUNTERS) or any(type(v) is not int or v != 0 for v in counters.values()):
        raise ValueError('safety_counters_unobserved')
    coins = []
    for seq, opportunity_hash, op_json in office.execute('SELECT seq,row_hash,payload_json FROM opportunity_log WHERE epoch_id=? AND seq<=? ORDER BY seq', (epoch_id, public_heads['opportunity_log'][0])):
        opportunity = json.loads(op_json)
        if not manifest['opened_at'] <= opportunity.get('first_eligible_at', float('nan')) < manifest['ended_at']:
            raise ValueError('opportunity_outside_epoch')
        rows = evaluation.execute("SELECT payload_json FROM grades WHERE candidate_seq=? AND epoch_id=? AND kind='coin_economics_v1' AND json_extract(payload_json,'$.look_id')=? AND seq<=?", (seq, epoch_id, manifest['look_id'], private_heads['grades'][0])).fetchall()
        if len(rows) != 1:
            raise ValueError('opportunity_grade_missing_or_duplicate')
        row = json.loads(rows[0][0])
        horizon_at = opportunity['first_eligible_at'] + economic['horizon_s']
        if (row.get('grading_contract') != ECONOMIC_CONTRACT
                or not _finite(row.get('horizon_at')) or row['horizon_at'] != horizon_at
                or horizon_at > manifest['look_at'] or row.get('look_at') != manifest['look_at']
                or row.get('metric_registry_hash') != manifest['metric_registry_hash']):
            raise ValueError('grade_horizon_unresolved')
        if row.get('entered') is None or row.get('opportunity_hash') != opportunity_hash or row.get('bound_usd_per_fill') != economic['bound_usd_per_fill'] or row.get('shift_id') not in shifts:
            raise ValueError('grade_evidence_unresolved')
        coins.append(dict(id=row['coin_id'], **{k: row[k] for k in ('day', 'y', 'n', 'entered', 'y_censored', 'n_censored', 'shift_id')}))
    if not coins or not manifest.get('stopping_rule'):
        raise ValueError('empty_or_unregistered_look')
    from .equity_snapshot import read_equity_snapshot
    strict = read_equity_snapshot(config_root, manifest.get('equity_snapshot'), epoch_id=epoch_id,
                                  look_at=manifest['look_at'], authenticate_snapshot=authenticate_snapshot, runtime_dir=runtime_dir)
    return dict(coins=coins, params=dict(params, cost=cost['amount_usd']), cost=cost['amount_usd'],
                exec_bound_present=True, counters=dict(counters), strict_equity=strict,
                proofs=dict(metric_registry_hash=manifest['metric_registry_hash'], calibration_proof_id=calibration['proof_id'],
                            stopping_rule=manifest['stopping_rule'], n_plan=params['n_plan'], block_days=params['block_days'],
                            alpha_support=[params['k']], design=dict(calibration['params']), exec_bound_evidence_id=bound['id'], cost_rule_id=RULE_ID,
                            cost_rule_hash=manifest['cost_rule_hash'], cost_days=cost['days'], gaps=gaps,
                            cut_heads={'office': public_heads, 'evaluation': private_heads},
                            cut_counts={'eligible_coins': len(coins), 'economic_grades': len(coins)}, strict_equity=strict['proofs']))


def validate_null_evidence(doc, shift, *, opened_at, epoch_id, shift_config):
    """Recompute prior filled-attempt latency by its eligible-time shift, without borrowing samples."""
    import statistics
    from sniper.office.shifts import shift_at
    samples, ids = doc.get('latency_samples_s'), doc.get('attempt_ids')
    eligible, filled = doc.get('first_eligible_at'), doc.get('filled_at')
    if not isinstance(samples, list) or len(samples) < 30 or any(not _finite(v) or v < 0 for v in samples) or any(not isinstance(values, list) or len(values) != len(samples) for values in (ids, eligible, filled)):
        raise ValueError('null_latency_insufficient')
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('duplicate_null_latency_attempt')
    if doc.get('shift_id') != shift['shift_id'] or doc.get('source_kind') not in ('discovery', 'prior_epoch') or (doc['source_kind'] == 'prior_epoch' and (not doc.get('source_epoch_id') or doc['source_epoch_id'] == epoch_id)) or not _finite(doc.get('known_through_at')) or doc['known_through_at'] >= opened_at:
        raise ValueError('null_latency_not_prior')
    for start, end, latency in zip(eligible, filled, samples):
        if not _finite(start) or not _finite(end) or not start <= end <= doc['known_through_at'] or abs(end - start - latency) > 1e-9 or shift_at(shift_config, start)['id'] != shift['shift_id']:
            raise ValueError('null_latency_sample_mismatch')
    total = doc.get('submitted_count')
    if type(total) is not int or total < len(samples) or not _finite(doc.get('filled_share')) or abs(doc['filled_share'] - len(samples) / total) > 1e-9:
        raise ValueError('null_filled_share_unknown')
    if shift.get('sample_n') != len(samples) or abs(statistics.median(samples) - shift['l_null_s']) > 1e-9:
        raise ValueError('null_latency_median_mismatch')
    return set(ids)


def release_aggregate(office, *, epoch_id, kind, released_at, aggregates, source_heads, authorize_release, credit_registry_path=None):
    """Publish only approved aggregate fields; raw evaluator evidence never enters the public store."""
    from sniper.office.store import APPEND_ONLY_TABLES, append, canonical_json, write_transaction
    aggregates = json.loads(canonical_json(aggregates))
    source_heads = json.loads(canonical_json(source_heads))
    fields = {'state', 'opportunity_count', 'entered_count', 'censored_count', 'unpriced_count',
              'net_pnl_usd', 'null_pnl_usd', 'cost_usd', 'office_net_usd', 'known_liq_lower_usd','strict_equity'}
    credit=kind=='descriptive_credit'
    if credit:
        fields={'agent_id','metric','value','sample_n','note','epoch_id','epoch_kind','look_id','censored_n','additive','top3_coin_share'}
    if not isinstance(aggregates, dict) or set(aggregates) - fields:
        raise ValueError('raw_or_unknown_aggregate_field')
    for key, value in (() if credit else aggregates.items()):
        if key == 'strict_equity':
            continue  # Validated against the epoch under the release transaction below.
        if key == 'state':
            if value not in ('known', 'partial', 'unknown', 'no_verdict', 'pass', 'fail', 'discovery'):
                raise ValueError('invalid_aggregate_state')
        elif value is not None and (not _finite(value) or (key.endswith('_count') and (type(value) is not int or value < 0))):
            raise ValueError('invalid_aggregate_value')
    if kind not in ('epoch_economics', 'discovery_status', 'auditor_status', 'descriptive_credit') or not isinstance(epoch_id, str) or not epoch_id or not _finite(released_at) or not callable(authorize_release):
        raise ValueError('invalid_release_identity')
    if not isinstance(source_heads, dict) or not source_heads or set(source_heads) - APPEND_ONLY_TABLES or any(not isinstance(v, str) or len(v) != 64 or any(c not in '0123456789abcdef' for c in v) for v in source_heads.values()):
        raise ValueError('invalid_release_source_heads')
    row = dict(epoch_id=epoch_id, kind=kind, released_at=released_at, aggregates=aggregates, source_heads=source_heads)
    with write_transaction(office):
        if authorize_release(epoch_id, kind, released_at) is not True:
            raise ValueError('release_not_authorized')
        epoch_row = office.execute('SELECT payload_json FROM epochs WHERE id=?', (epoch_id,)).fetchone()
        epoch = json.loads(epoch_row[0]) if epoch_row else {}
        if kind == 'epoch_economics' and epoch.get('kind') == 'evaluated' and 'strict_equity' not in aggregates:
            raise ValueError('strict_equity_required')
        if 'strict_equity' in aggregates:
            from .equity_snapshot import validate_result
            validate_result(aggregates['strict_equity'], epoch_id=epoch_id, look_at=epoch.get('registered_look_at'))
        if credit:
            _validate_credit_release(office,epoch_id,released_at,aggregates,credit_registry_path)
        sql="SELECT payload_json,row_hash FROM sealed_views WHERE epoch_id=? AND json_extract(payload_json,'$.kind')=? AND json_extract(payload_json,'$.released_at')=?"
        args=[epoch_id,kind,released_at]
        if credit:
            sql+=" AND json_extract(payload_json,'$.aggregates.agent_id')=? AND json_extract(payload_json,'$.aggregates.metric')=?"
            args.extend((aggregates['agent_id'],aggregates['metric']))
        prior=office.execute(sql+' ORDER BY seq DESC LIMIT 1',args).fetchone()
        if prior:
            if canonical_json(json.loads(prior[0])) != canonical_json(row):
                raise ValueError('frozen_release_changed')
            return prior[1]
        return append(office, 'sealed_views', row)


def _validate_credit_release(office,epoch_id,released_at,a,registry_path):
    """Trusted sealer supplies coin-deduplicated metrics; a model never supplies this authority."""
    required={'agent_id','metric','value','sample_n','note','epoch_id','epoch_kind','look_id','censored_n','additive'}
    if not required<=set(a) or a['epoch_id']!=epoch_id or a['additive'] is not False or any(not isinstance(a[k],str) or not a[k].strip() for k in ('agent_id','metric','note')) or (a['value'] is not None and not _finite(a['value'])) or any(type(a[k]) is not int or a[k]<0 for k in ('sample_n','censored_n')) or a['censored_n']>a['sample_n']:
        raise ValueError('invalid_descriptive_credit')
    if 'top3_coin_share' in a and a['top3_coin_share'] is not None and (not _finite(a['top3_coin_share']) or not 0<=a['top3_coin_share']<=1):
        raise ValueError('invalid_credit_tail_share')
    raw=office.execute('SELECT payload_json FROM epochs WHERE id=?',(epoch_id,)).fetchone()
    epoch=json.loads(raw[0]) if raw else {}
    if epoch.get('epoch_kind') not in ('evaluated','discovery') or epoch['epoch_kind']!=a['epoch_kind']:
        raise ValueError('credit_epoch_kind_mismatch')
    if a['epoch_kind']=='evaluated':
        if not isinstance(a['look_id'],str) or not a['look_id'] or a['look_id']!=epoch.get('registered_look_id') or not _finite(epoch.get('registered_look_at')) or released_at<epoch['registered_look_at']:
            raise ValueError('credit_look_not_released')
    elif a['look_id'] is not None:
        raise ValueError('discovery_credit_has_look')
    if registry_path is None or not isinstance(epoch.get('metric_registry_hash'),str):
        raise ValueError('credit_registry_not_frozen')
    registry=load_registry(registry_path,epoch['metric_registry_hash'])
    metrics=registry.get('credit_metrics')
    if not isinstance(metrics,list) or not metrics or a['metric'] not in metrics:
        raise ValueError('unknown_credit_metric')

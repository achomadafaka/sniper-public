"""Frozen per-wallet-chain rounds and full-census paired diagnostics.

Registration is not promotion. A fixed read with missing execution, power or
independent statistical review remains ineligible; never a max-of-k winner.
"""
from datetime import datetime,timezone
import json
import math
import statistics
from collections import Counter
from .evidence import PROTOCOL,digest,number
from .replay import replay
from sniper.persistence.db_wallet_execution import encoded


# The tail must outlast the longest wait+hold path AND the time an outcome takes to become a
# readable row after it (Codex W8: a tail equal to the hold sealed an origin whose settlement landed
# 2.9 s later; a flat 60 s allowance still let an accepted 61 s entry delay settle after the read).
# A round registers each maximum (latency_bounds), the tail must cover their sum, and an episode
# slower than its plan's bounds is censored. persistence_s is a REGISTERED bound, not a measured one:
# no episode producer exists yet (2026-10-07), so nothing checks a producer frontier before the read.
LATENCY_BOUNDS = ('entry_delay_s', 'exit_delay_s', 'settlement_s', 'persistence_s')


def contract_problem(plan):
    """Why a plan's outcome tail cannot cover its own longest path, or None. Applied at seal and again
    to a stored plan before its first read: an older rule never decides today's read."""
    if not number(plan.get('outcome_tail_s')):
        return 'outcome_tail_missing'
    bounds = plan.get('latency_bounds')
    if not isinstance(bounds, dict) or not all(number(bounds.get(k)) for k in LATENCY_BOUNDS):
        return 'latency_bounds_unregistered'
    policies = [plan.get('baseline'), *(plan.get('challengers') or [])]
    try:
        longest = max(1800., max(float(p.get('max_wait_s', 0)) + float(p['max_hold_s']) for p in policies))
    except (AttributeError, KeyError, TypeError, ValueError):
        return 'policy_path_unknown'
    if plan['outcome_tail_s'] < longest + sum(bounds[k] for k in LATENCY_BOUNDS):
        return 'outcome_tail_short'
    return None


def seal_round(db, plan, *, now):
    if plan.get('protocol')!=PROTOCOL or not plan.get('wallet') or not plan.get('chain'):
        raise ValueError('wallet-chain protocol required')
    challengers=plan.get('challengers')
    if not isinstance(challengers,list) or not 0<=len(challengers)<=8:
        raise ValueError('at most eight challengers per frozen round')
    policies=[plan.get('baseline'),*challengers]
    if any(not isinstance(p,dict) or not p.get('policy_id') for p in policies): raise ValueError('policy identity required')
    if len({p['policy_id'] for p in policies})!=len(policies): raise ValueError('policy identities must be unique')
    try:
        de,vs,ve,ts,te,embargo=(float(plan[k]) for k in ('discovery_end','validation_start','validation_end','test_start','test_end','embargo_s'))
        if not all(math.isfinite(x) for x in (de,vs,ve,ts,te,embargo,now)): raise ValueError
        if not de<=now<vs<=ve<ts<te: raise ValueError
        longest=max(1800.,max(float(p.get('max_wait_s',0))+float(p['max_hold_s']) for p in policies))
        if embargo<longest or vs-de<embargo or ts-ve<embargo: raise ValueError
        # Mandatory (Codex W8): without a tail the fixed read froze the last origins' still-accruing
        # outcomes as missing, forever. It must cover the longest path, delays and persistence included.
        if contract_problem(plan): raise ValueError
    except (KeyError,TypeError,ValueError): raise ValueError('round must be sealed before a purged future validation/test') from None
    payload={**plan,'sealed_at':now,'authority':'paper_only','purpose':'prospective_execution_research'}
    key=digest(payload)
    with db: db.execute('INSERT OR IGNORE INTO we_studies VALUES (?,?)',(key,encoded(payload)))
    return key


def evaluate_round(db, study_id, *, now):
    row=db.execute('SELECT payload FROM we_studies WHERE study_id=?',(study_id,)).fetchone()
    if row is None: raise ValueError('unknown sealed study')
    plan=json.loads(row[0]); baseline=plan['baseline']; policies=[baseline,*plan['challengers']]
    base={'study_id':study_id,'wallet':plan['wallet'],'chain':plan['chain'],
          'eligible':False,'winner':None,'basis':'paired_execution_diagnostic','funded_authority':False}
    # No interim test outcome peeking. Re-reading the fixed final receipt cannot
    # silently fold in newly collected rows or reuse the holdout for a new winner.
    problem=contract_problem(plan)                      # the stored plan, under today's rule
    fixed=db.execute('SELECT payload FROM we_study_reads WHERE study_id=?',(study_id,)).fetchone()
    if problem:
        if fixed:                                       # never reused as a valid holdout
            old=json.loads(fixed[0])
            return {**old,'status':'invalid','codes':[*(old.get('codes') or []),'contract_invalid',problem]}
        return {**base,'status':'blocked','codes':['contract_invalid',problem]}
    read_after=plan['test_end']+plan['outcome_tail_s']
    if now<read_after: return {**base,'status':'accruing','test_end':plan['test_end'],'read_after':read_after}
    if fixed: return json.loads(fixed[0])
    bounds=plan['latency_bounds']
    iso=lambda t: datetime.fromtimestamp(t,timezone.utc).isoformat()
    origins=[dict(r) for r in db.execute('SELECT origin_id,mint,observed_at FROM we_origins WHERE wallet=? AND chain=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at,origin_id',
              (plan['wallet'],str(plan['chain']),iso(plan['test_start']),iso(plan['test_end'])))]
    paired=[]; outcomes={p['policy_id']:[] for p in policies}; codes=['validation_missing']
    censored={p['policy_id']:Counter() for p in policies}
    regimes={'normal','thin','hot_broad','hot_narrow','unknown'}
    context_labels={'market_regime':regimes, 'chain_market_regime':regimes,
                    'feed_health':{'ok','partial','stale','unavailable','unknown'}}
    contexts={name:Counter() for name in context_labels}
    for origin in origins:
        raw=db.execute('SELECT payload FROM we_episodes WHERE origin_id=?',(origin['origin_id'],)).fetchone()
        e=json.loads(raw[0]) if raw else {}
        try:
            clock=datetime.fromisoformat(origin['observed_at'])
            observed=clock.timestamp() if clock.tzinfo is not None else None
        except (TypeError,ValueError): observed=None
        source=e.get('source')
        reason=None
        if raw is None: reason='episode_missing'
        elif (e.get('origin_id')!=origin['origin_id'] or e.get('mint')!=origin['mint']
              or e.get('wallet')!=plan['wallet'] or str(e.get('chain'))!=str(plan['chain'])
              or observed is None or not isinstance(source,dict) or not number(source.get('received_at'))
              or abs(source['received_at']-observed)>.001): reason='origin_identity_or_clock_mismatch'
        if reason is None:
            ex=e.get('execution') or {}
            delays=('entry_delay_s','exit_delay_s','settlement_s')
            if not all(number(ex.get(k)) for k in delays): reason='latency_unknown'
            elif any(ex[k]>bounds[k] for k in delays):
                reason='latency_outside_registered_bound'      # it could settle after read_after
        features=(e.get('features') or {}) if reason is None else {}
        for name, labels in context_labels.items():
            feature=features.get(name) if isinstance(features,dict) else None
            value=feature.get('value') if isinstance(feature,dict) else None
            available=feature.get('available_at') if isinstance(feature,dict) else None
            label=value if isinstance(value,str) and value in labels and number(available) and available<=observed else 'unknown'
            contexts[name][label]+=1
        if reason:
            for p in policies: censored[p['policy_id']][reason]+=1
            continue
        results=[replay(e,p) for p in policies]
        for p,r in zip(policies,results):
            if r['net_cash_usd'] is None: censored[p['policy_id']][r.get('reason','outcome_unknown')]+=1
        if any(r['net_cash_usd'] is None for r in results): continue
        paired.append(origin)
        for p,result in zip(policies,results): outcomes[p['policy_id']].append(result)
    excluded=len(origins)-len(paired)
    if excluded: codes.append('outcome_censored')
    if not origins: codes.append('cohort_invalid')
    reports=[]
    for p in policies:
        values=[r['net_cash_usd'] for r in outcomes[p['policy_id']]]
        deltas=[v-r['net_cash_usd'] for v,r in zip(values,outcomes[baseline['policy_id']])]
        reports.append({'policy_id':p['policy_id'],'priced_origins':len(values),
                        'net_cash_per_registered_origin':sum(values)/len(origins) if origins and not excluded else None,
                        'paired_mean_delta':statistics.fmean(deltas) if deltas else None,
                        'median_cash':statistics.median(values) if values else None,
                        'worst_cash':min(values) if values else None,
                        'not_entered':sum(r['state']=='not_entered' for r in outcomes[p['policy_id']]),
                        'unsellable':sum(r['state']=='unsellable' for r in outcomes[p['policy_id']]),
                        'locked_capital_usd':sum(r['locked_capital_usd'] for r in outcomes[p['policy_id']]),
                        'censor_reasons':dict(censored[p['policy_id']]),
                        'comparison_population':'complete_pairs_only',
                        'conclusion':'independent power, coin/day uncertainty and cost/delay stress review required'})
    result={**base,'status':'blocked','codes':codes,'registered_origins':len(origins),
            'paired_origins':len(paired),'excluded_origins':excluded,
            'distinct_coins':len({r['mint'] for r in paired}),
            'distinct_days':len({r['observed_at'][:10] for r in paired}),
            'policies':reports,'context_counts':{k:dict(v) for k,v in contexts.items()},
            'census_digest':digest(origins),'read_at':now,'holdout_read_count':1}
    with db: db.execute('INSERT INTO we_study_reads VALUES (?,?)',(study_id,encoded(result)))
    return result

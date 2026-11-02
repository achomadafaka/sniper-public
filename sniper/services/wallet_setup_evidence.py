"""Read-only setup evidence. No authority, selector, sizing or policy mutation."""
from collections import defaultdict
from datetime import datetime,timezone
import math
import json
from sniper.services.memecoin_chains import canonical_chain_id,resolve_chain
from sniper.services.memecoin_paper_report import _parse


def summarize_setup(rows,*,chain,uid,setup,arms,cutoff):
    chain=canonical_chain_id(chain);cut=_parse(cutoff)
    if chain is None or cut is None or not arms or len(set(arms))!=len(arms):
        raise ValueError('canonical chain, unique arms and aware cutoff required')
    selected=[r for r in rows if canonical_chain_id(r['chain'])==chain and r['uid']==uid and r['setup']==setup
              and _parse(r.get('opened_at')) is not None and _parse(r['opened_at'])>cut]
    groups=defaultdict(list)
    for r in selected: groups[(r.get('cohort_id'),r['origin'])].append(r)
    counters=dict(origins=len(groups),complete_origins=0,open_origins=0,censored_origins=0,
                  missing_arm_origins=0,duplicate_origins=0,late_setup_origins=0,policy_mismatch_origins=0)
    complete=[];evidence=[]
    ids=defaultdict(set)
    for r in selected: ids[(r.get('cohort_id'),r['arm'])].add(r.get('policy_id'))
    cohorts={r.get('cohort_id') for r in selected}
    for (cohort,origin),group in groups.items():
        counts={a:sum(r['arm']==a for r in group) for a in arms}
        reason=None
        if any(n>1 for n in counts.values()): reason='duplicate_origins'
        elif any(n==0 for n in counts.values()): reason='missing_arm_origins'
        elif any(len(ids[(cohort,a)])!=1 or None in ids[(cohort,a)] for a in arms) or not cohort:
            reason='policy_mismatch_origins'
        elif any(_parse(r.get('known_at')) is None or _parse(r['known_at'])>_parse(r['opened_at']) for r in group):
            reason='late_setup_origins'
        elif any(not r['closed'] for r in group): reason='open_origins'
        elif any(r.get('censored') or r.get('net_pnl') is None or not _finite(r.get('net_pnl')) or
                 not _finite(r.get('notional')) or r['notional']<=0 for r in group): reason='censored_origins'
        if reason: counters[reason]+=1
        else:
            complete.extend(r for r in group if r['arm'] in arms)
            counters['complete_origins']+=1
        evidence.append(dict(origin=origin,cohort=cohort,state=reason or 'complete',mint=group[0]['mint']))
    policies={}
    for arm in arms:
        subset=[r for r in complete if r['arm']==arm]
        coins=defaultdict(lambda:[0.0,0.0])
        for r in subset:
            coins[r['mint']][0]+=r['net_pnl']; coins[r['mint']][1]+=r['notional']
        n=sum(r['notional'] for r in subset);pnl=sum(r['net_pnl'] for r in subset)
        absolute=sum(abs(v[0]) for v in coins.values())
        best=max(coins,key=lambda c:coins[c][0]) if coins else None
        rest=[r for r in subset if r['mint']!=best]
        rn=sum(r['notional'] for r in rest)
        policies[arm]=dict(notional=n if len(cohorts)<=1 else None,net_pnl=pnl if subset and len(cohorts)==1 else None,
                           **{'return':pnl/n if n and len(cohorts)==1 else None},
                           top_coin_share=max((abs(v[0]) for v in coins.values()),default=0)/absolute if absolute and len(cohorts)==1 else None,
                           leave_best_coin_out_return=sum(r['net_pnl'] for r in rest)/rn if rn and len(cohorts)==1 else None,
                           uncertainty='coin-cluster uncertainty not estimated; no funding verdict',
                           priced=len(subset),open=sum(not r['closed'] for r in selected if r['arm']==arm),
                           cost_state='known for complete rows; otherwise unknown')
    full_book={a:dict(rows=sum(r['arm']==a for r in selected),
                      priced=sum(r['arm']==a and r['closed'] and _finite(r.get('net_pnl')) and not r.get('censored') for r in selected),
                      open=sum(r['arm']==a and not r['closed'] for r in selected),
                      censored=sum(r['arm']==a and r['closed'] and (r.get('censored') or not _finite(r.get('net_pnl'))) for r in selected)) for a in arms}
    policy_ids={a:sorted({r['policy_id'] for r in selected if r['arm']==a and r.get('policy_id')}) for a in arms}
    return dict(counters,full_book=full_book,policy_ids=policy_ids,chain=chain,uid=uid,setup=setup,cutoff=cutoff,arms=list(arms),
                distinct_coins=len({r['mint'] for r in complete}),policies=policies,evidence=evidence,
                cohort_state='single' if len(cohorts)<=1 else 'multiple; metrics not pooled',
                state='diagnostic',authority='none')


def _finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def project_setup_evidence(ledger,*,chain,arms,cutoff):
    """Reuse the paper report's outcome and eligibility projection, keeping missing setup evidence."""
    from sniper.services.memecoin_paper_report import _project_rows,_add_outcome,_exact_groups
    from sniper.services.memecoin_policy import paper_policy_is_valid
    now=datetime.now(timezone.utc)
    projected=[_add_outcome(r,now) for r in _project_rows(ledger,resolve_chain(chain))]
    eligible_ids={r['id'] for _,g in _exact_groups(projected,arms,forward=True) for r in g}
    adapted=[]
    for r in projected:
        if not r.get('origin_event_id') or not r.get('source_user_id'): continue
        snap=ledger.execute('SELECT decided_at,features_json,data_quality_json FROM memecoin_entry_snapshots '
                            'WHERE upstream_event_id=? AND chain IN (?,?)',
                            (r['origin_event_id'],canonical_chain_id(chain),resolve_chain(chain))).fetchone()
        features=json.loads(snap[1] or '{}') if snap else {}
        venue=features.get('venue') or features.get('venue_kind') or 'unknown'
        quote=features.get('quote_asset') or 'unknown'
        setup=f'{venue}:{quote}'
        adapted.append(dict(origin=r['origin_event_id'],chain=chain,uid=r['source_user_id'],mint=r['mint'],setup=setup,
                            cohort_id=r.get('paper_cohort_id'),policy_id=r.get('assigned_policy_id'),policy_json=r.get('assigned_policy_json'),arm=r['strategy'],
                            known_at=snap[0] if snap else None,opened_at=r['opened_at'],closed=not r['is_open'],
                            censored=r['id'] not in eligible_ids or not paper_policy_is_valid(r) or r['report_pnl'] is None,
                            notional=r.get('entry_notional'),net_pnl=None,modeled_pnl=r.get('report_pnl'),
                            costs_state='not independently recorded'))
    # Explicit costs are unavailable in legacy paper projection: no invented net-fit claim.
    result=defaultdict(list)
    for uid,setup in sorted({(r['uid'],r['setup']) for r in adapted}):
        summary=summarize_setup(adapted,chain=chain,uid=uid,setup=setup,arms=arms,cutoff=cutoff)
        summary['price_kind']='legacy paper model; net costs unknown'
        summary.update(frozen_setup_policies(adapted,uid=uid,setup=setup,arms=arms,cutoff=cutoff))
        summary['latency_state']='not independently recorded in legacy paper rows'
        summary['route_state']='not independently recorded in legacy paper rows'
        result[uid].append(summary)
    return dict(result)


def frozen_setup_policies(rows,*,uid,setup,arms,cutoff):
    selected=[r for r in rows if r['uid']==uid and r['setup']==setup and r['arm'] in arms
              and _parse(r.get('opened_at')) is not None and _parse(r['opened_at'])>_parse(cutoff)]
    policies=[dict(arm=r['arm'],policy_id=r.get('policy_id'),policy_json=r.get('policy_json'),
                   cohort_id=r.get('cohort_id')) for r in selected]
    policies=list({json.dumps(p,sort_keys=True):p for p in policies}.values())
    exportable=all(len([p for p in policies if p['arm']==arm])==1 for arm in arms) and bool(arms)
    exportable=exportable and all(p['policy_id'] and p['policy_json'] and p['cohort_id'] for p in policies)
    return dict(frozen_policies=policies,exportable=bool(exportable))


def blocker_evidence(attempts,captures,*,origin):
    actual=[a for a in attempts if a.get('origin')==origin]
    observed=[c for c in captures if c.get('origin')==origin]
    kinds={c.get('kind') for c in observed}
    kind='exact_quote' if kinds=={'exact_quote'} else 'bar_model' if kinds=={'bar_model'} else 'unknown'
    return dict(origin=origin,actual_gate=list(dict.fromkeys(a.get('gate') for a in actual)),
                inputs_state=('complete' if actual and all(a.get('inputs_state')=='complete' for a in actual)
                              else 'partial' if actual else 'unknown'),
                capture_state='ok' if observed and all(c.get('state')=='ok' for c in observed) else 'unknown',
                outcome_kind=kind,affected=bool(actual),attempts=actual,captures=observed)


def freeze_adjustment(*,chain,uid,cutoff,setup,arms,change):
    from sniper.persistence.db_memecoin import EXEMPTIBLE_GATE_FAMILIES
    chain=canonical_chain_id(chain)
    fields=change.get('fields')
    if (chain is None or not uid or not isinstance(setup,str) or not setup.strip() or _parse(cutoff) is None or not arms or len(set(arms))!=len(arms) or
        change.get('category')!='selection' or not isinstance(fields,dict) or len(fields)!=1 or
        not set(fields)<=EXEMPTIBLE_GATE_FAMILIES):
        raise ValueError('one documented selection change required; safety controls cannot be exempted')
    value=next(iter(fields.values()))
    if not isinstance(value,bool) and not _finite(value):
        raise ValueError('finite numeric or boolean hypothesis required')
    return dict(version=1,chain=chain,uid=uid,cutoff=cutoff,setup=setup,arms=list(arms),change=change,
                state='unvalidated',authority='none',validation='later unchanged whole-origin paper required')


def project_blockers(ledger,*,chain,cutoff,capture_db=None):
    from sniper.services.memecoin_refusal_capture import classify_gate
    from sniper.services.memecoin_chains import network_ids_for
    ids=network_ids_for(resolve_chain(chain));result=defaultdict(list)
    rows=ledger.execute('SELECT origin_event_id,source_user_id,terminal_status,terminal_reason,trace_json '
                        'FROM memecoin_entry_attempts WHERE network_id IN ('+','.join('?' for _ in ids)+') '
                        'AND decision_started_at>=? ORDER BY attempt_id DESC LIMIT 1000',(*ids,cutoff)).fetchall()
    grouped=defaultdict(list)
    for r in rows:
        if not r[0] or not r[1]: continue
        gate=classify_gate(r[3] or '')
        if gate is None: continue
        grouped[(r[1],r[0])].append(dict(origin=r[0],gate=gate,inputs_state='partial',
                                        status=r[2],reason=r[3],trace=blocker_trace(json.loads(r[4] or '{}'),gate),
                                        provenance='immutable funded attempt'))
    for (uid,origin),attempts in grouped.items():
        result[uid].append(blocker_evidence(attempts,refusal_facts(capture_db,chain=chain,origin=origin)
                                           if capture_db is not None else [],origin=origin))
    return dict(result)


def refusal_facts(capture_db,*,chain,origin):
    """Stored route/display observations; display prices never become executable evidence."""
    from sniper.services.memecoin_chains import network_ids_for
    ids=network_ids_for(resolve_chain(chain))
    rows=capture_db.execute('SELECT capture_id,mint,observed_at,age_s,quote_price_basis,quote_side,'
        'quote_price,quote_input_usd,token_qty_atomic,quote_output_atomic,quote_min_output_atomic,quality,outcome '
        'FROM memecoin_refusal_captures WHERE origin_event_id=? AND network_id IN ('+
        ','.join('?' for _ in ids)+') ORDER BY observed_at LIMIT 101',(origin,*ids)).fetchall()
    return [dict(origin=origin,capture_id=r[0],mint=r[1],at=r[2],age_s=r[3],
                 kind='exact_quote' if r[4]=='exact_route' else 'display_mark' if r[4]=='display' else 'unknown',
                 side=r[5],price=r[6],input_usd=r[7],quantity_atomic=r[8],output_atomic=r[9],floor_atomic=r[10],
                 state='ok' if r[2] is not None and r[6] is not None and r[4]=='exact_route' else 'partial',
                 quality=r[11],outcome=r[12],coverage='truncated' if len(rows)>100 else 'stored observations')
            for r in rows[:100]]


def blocker_trace(trace,gate):
    # Retain actual gate inputs; unrelated signing/RPC/shadow payloads are not blocker evidence.
    stages=trace.get('substage_events',[])
    provenance=next((s.get('gate_provenance_v1') for s in stages if s.get('gate_provenance_v1')),None)
    return dict(gate=gate,decision_started_at=trace.get('decision_started_at'),
                inputs=(provenance or {}).get('inputs'),
                evaluation=(provenance or {}).get('gate_evaluations',{}).get(gate),
                source_evidence=trace.get('source_evidence'))


# Raw historical measurements remain readable; hypothetical conclusions are ineligible.
from sniper.research.wallet_execution.publication import diagnostic_result as _diagnostic_result
summarize_setup = _diagnostic_result(summarize_setup)
project_setup_evidence = _diagnostic_result(project_setup_evidence)

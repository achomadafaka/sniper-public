"""Bounded local archival and wallet-chain coverage. Zero network or funded writes."""
from __future__ import annotations
import json
import sqlite3
from contextlib import closing
from sniper.persistence.db_core import get_readonly_connection
from sniper.persistence import db_wallet_execution as store
from sniper.research.wallet_execution.evidence import PROTOCOL, digest, number

GAPS = ['verified source swap and address', 'complete ordered one-second route state',
        'exact entry and whole-bag exits', 'prior latency and full costs', 'sealed future comparison']


def roster_manifest(rows, *, now, known_cells=()):
    from sniper.services.memecoin_chains import canonical_chain_id
    def chain_id(value): return canonical_chain_id(value) or str(value)
    members=[]
    for row in rows:
        if row.get('paused') or row.get('excluded') or not row.get('source_user_id'): continue
        uid=str(row['source_user_id']); armed=bool(row.get('real_approved'))
        from sniper.persistence.db_memecoin import _parse_real_chains
        raw=row.get('real_chains') or ''
        if armed and str(raw).lstrip().startswith('['):
            try: chains=json.loads(raw)
            except (ValueError,TypeError):chains=[]
        else: chains=sorted(_parse_real_chains(raw)) if armed else []
        if not isinstance(chains,list):chains=[]
        observed={chain_id(chain) for wallet,chain in known_cells if wallet==uid}
        all_chains={chain_id(c) for c in chains if str(c)} | observed
        if not all_chains: all_chains={'unknown'}
        for chain in sorted(all_chains):
            authority=armed and chain in {chain_id(c) for c in chains}
            members.append({'wallet':uid, 'handle':row.get('handle') or uid, 'chain':chain,
                            'mode':'armed' if authority else 'watch',
                            'nomination':'current_operator_authority' if authority else 'watch_only_not_nominated'})
    return {'protocol':PROTOCOL, 'sealed_at':now, 'members':sorted(members,key=lambda r:(r['wallet'],r['chain'])),
            'purpose':'coverage_only', 'funded_authority':False}


def source_page(db, source, path, name, table, key, limit, *, bootstrap):
    """An anchor detects loss or change at the cursor; it cannot prove every historical row is unchanged. Missing anchor is a gap.

    Device/inode detects replacement. Retention, updates or resets create a new
    archive epoch and an explicit gap; no absent history is reconstructed.
    """
    stat=path.stat(); identity=f'{stat.st_dev}:{stat.st_ino}'
    cp=store.checkpoint(db,name)
    top=source.execute(f'SELECT max({key}) FROM {table}').fetchone()[0] or 0
    offset=cp['offset'] if cp else (0 if bootstrap=='beginning' else top)
    gap=None
    if cp:
        old=source.execute(f'SELECT * FROM {table} WHERE {key}=?',(offset,)).fetchone() if offset else None
        if cp['identity']!=identity or top<offset or (offset and (old is None or digest(dict(old))!=cp['anchor'])):
            gap='retained_replaced_or_changed_source';offset=0
    epoch=digest({'name':name,'identity':identity,'prior':cp['seq'] if cp else None}) if gap or not cp else cp['epoch']
    rows=[dict(r) for r in source.execute(f'SELECT * FROM {table} WHERE {key}>? ORDER BY {key} LIMIT ?',(offset,limit))]
    if rows: offset=rows[-1][key]
    anchor_row=source.execute(f'SELECT * FROM {table} WHERE {key}=?',(offset,)).fetchone() if offset else None
    anchor=digest(dict(anchor_row)) if anchor_row else None
    return rows,(name,offset,identity,anchor,epoch),gap,top-offset


def collect_once(db, source_path, *, now, limit=500, bootstrap='tail', tape_path=None, volume_path=None):
    from pathlib import Path
    limit=max(1,min(int(limit),2000))
    status={'source_status':'ok','new_origins':0,'archived_ticks':0,'provider_requests':0,'at':now,'continuity_gaps':[],'conflicting_origins':0}
    try:
        with closing(get_readonly_connection(str(source_path))) as source:
            source.execute('PRAGMA busy_timeout=1000')
            roster=[dict(r) for r in source.execute('SELECT source_user_id,handle,paused,excluded,real_approved,real_chains FROM memecoin_wallets')]
            first=store.checkpoint(db,'feed') is None
            rows,cp,gap,backlog=source_page(db,source,Path(source_path),'feed','memecoin_feed_observations_v2','id',limit,bootstrap=bootstrap)
        with db:
            if first: status['legacy_gap']='not_enrolled_before_start' if bootstrap=='tail' else 'not_enrolled_before_source_history'
            if gap:
                status['continuity_gaps'].append('feed')
                db.execute('INSERT INTO we_source_gaps(source,at,reason) VALUES (?,?,?)',('feed',now,gap))
            for row in rows:
                if row.get('normalized_side')=='buy':
                    old=db.execute('SELECT 1 FROM we_origins WHERE origin_id=?',(digest({'feed_id':row['upstream_event_id']}),)).fetchone()
                    try:
                        store.record_origin(db,row);status['new_origins']+=int(old is None)
                    except ValueError:
                        # Preserve original denominator/identity and retain conflicting
                        # bytes as quarantine evidence without retrying forever.
                        store.record_raw(db,'feed_conflict:'+cp[-1],digest(row),row)
                        db.execute('INSERT INTO we_source_gaps(source,at,reason) VALUES (?,?,?)',('feed',now,'immutable_origin_conflict'))
                        status['conflicting_origins']+=1
            store.record_checkpoint(db,*cp)
            known=[(r['wallet'],r['chain']) for r in db.execute('SELECT wallet,chain FROM we_cells')]
            manifest=roster_manifest(roster,now=now,known_cells=known)
            previous=db.execute('SELECT id,payload FROM we_manifests ORDER BY rowid DESC LIMIT 1').fetchone()
            status['manifest_id']=previous['id'] if previous and json.loads(previous['payload'])['members']==manifest['members'] else store.freeze_manifest(db,manifest)
            status['feed_backlog']=max(0,backlog)
    except (OSError,sqlite3.Error,ValueError) as exc:
        db.rollback();status.update(source_status='unavailable',reason=f'{type(exc).__name__}: {exc}',new_origins=0)
    if tape_path:
        try:
            pages=[]
            with closing(get_readonly_connection(str(tape_path))) as tape:
                tape.execute('PRAGMA busy_timeout=1000')
                for name,table,key in [('evm_ticks','pool_ticks','id'),('evm_heads','block_times','rowid'),('evm_gaps','tick_gaps','id')]:
                    # rowid is an archival insertion cursor, never a chain-order claim.
                    if key=='rowid':
                        table='(SELECT rowid AS archive_rowid,* FROM block_times)';key='archive_rowid'
                    pages.append(source_page(db,tape,Path(tape_path),name,table,key,limit,bootstrap='tail'))
            with db:
                for rows,cp,gap,backlog in pages:
                    name=cp[0]
                    if gap:
                        status['continuity_gaps'].append(name)
                        db.execute('INSERT INTO we_source_gaps(source,at,reason) VALUES (?,?,?)',(name,now,gap))
                    for row in rows: store.record_raw(db,name+':'+cp[-1],digest(row),row)
                    store.record_checkpoint(db,*cp)
                    status[name+'_backlog']=max(0,backlog)
                    if name=='evm_ticks':status['archived_ticks']=len(rows)
                # These shared tables permit updates behind an insertion cursor.
                # Bounded archive pages cannot authenticate revision completeness.
                if not db.execute("SELECT 1 FROM we_source_gaps WHERE source='evm_revisions' LIMIT 1").fetchone():
                    db.execute('INSERT INTO we_source_gaps(source,at,reason) VALUES (?,?,?)',('evm_revisions',now,'historical_mutation_coverage_unknown'))
                status.update(tape_status='sampled_state_not_executable_path',header_revision_coverage='unknown',gap_revision_coverage='unknown',archive_is_canonical=False)
        except (OSError,sqlite3.Error,ValueError) as exc:
            db.rollback();status.update(tape_status='unavailable',tape_reason=f'{type(exc).__name__}: {exc}')
    if volume_path is not None:
        from sniper.research.wallet_execution.ordered_capture import archive_ordered_once
        status['ordered_capture'] = archive_ordered_once(db, volume_path, now=now, origin_limit=limit)
    with db:db.execute('INSERT INTO we_runs(at,payload) VALUES (?,?)',(now,store.encoded(status)))
    return status


def build_coverage(db, *, now):
    latest=db.execute('SELECT id,payload FROM we_manifests ORDER BY rowid DESC LIMIT 1').fetchone()
    run=db.execute('SELECT payload FROM we_runs ORDER BY id DESC LIMIT 1').fetchone()
    status=json.loads(run[0]) if run else {'source_status':'unavailable'}
    members=json.loads(latest['payload'])['members'] if latest else []
    summary=store.coverage_counts(db)
    breadth={(r['wallet'],r['chain']):{k:r[k] for k in ('distinct_coins','observed_days','unknown_clock_buys')} for r in summary}
    counts={(r['wallet'],r['chain']):r['origins'] for r in summary}
    eligible={(r['wallet'],r['chain']):r['episodes'] for r in summary}
    # Registration is visible without reading an interim outcome or suggesting
    # that this local archive produces the missing quantity-specific quotes.
    studies=[]
    for row in db.execute('SELECT study_id,payload FROM we_studies ORDER BY study_id'):
        plan=json.loads(row['payload'])
        studies.append({'study_id':row['study_id'],'wallet':plan['wallet'],'chain':str(plan['chain']),
                        'analysis_scope':plan.get('analysis_scope'),
                        'state':'sealed_awaiting_execution_evidence','test_start':plan['test_start'],
                        'test_end':plan['test_end'],'read_after':(plan['test_end']+plan['outcome_tail_s'] if number(plan.get('outcome_tail_s')) else None),
                        'policies':[plan['baseline'],*plan['challengers']]})
    sealed={(s['wallet'],s['chain']) for s in studies}
    cells=[{**m,**breadth.get((m['wallet'],m['chain']), {'distinct_coins':0,'observed_days':0,'unknown_clock_buys':0}),
            'signals':counts.get((m['wallet'],m['chain']),0),
            'complete_episodes':eligible.get((m['wallet'],m['chain']),0),
            'status':'blocked','strategy':None,'missing':GAPS,
            'identity':'unproven','path':'not_complete',
            'study':'sealed' if (m['wallet'],m['chain']) in sealed else 'not_sealed'} for m in members]
    first=db.execute('SELECT payload FROM we_runs ORDER BY id LIMIT 1').fetchone()
    first_status=json.loads(first[0]) if first else {}
    return {'protocol':PROTOCOL,'generated_at':now,'manifest_id':latest['id'] if latest else None,
            'source_status':status.get('source_status','unavailable'), 'cells':cells,'studies':studies,
            'total_origins':sum(counts.values()),'provider_requests':0,
            'legacy_gap':first_status.get('legacy_gap','not_enrolled_before_start'),
            'source_state':status,'census_complete':False,
            'continuity_gap_count':db.execute('SELECT count(*) FROM we_source_gaps').fetchone()[0],
            'publication':'diagnostic_only','funded_authority':False}


def read_coverage(path, *, chain, wallet=None, now, max_age_s=120.):
    """Bounded artifact read for UI. No database or provider request."""
    from datetime import datetime
    from sniper.services.memecoin_chains import canonical_chain_id
    wanted=canonical_chain_id(chain) or str(chain)
    def epoch(value):
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None: raise ValueError('naive coverage clock')
        return dt.timestamp()
    empty={'state':'unavailable','cells':[],'publication':'diagnostic_only','funded_authority':False}
    try:
        if path.stat().st_size>2_000_000: return empty
        body=json.loads(path.read_text())
        if body.get('protocol')!=PROTOCOL or not isinstance(body.get('cells'),list): return empty
        age=epoch(now)-epoch(body['generated_at'])
        if age < -5: return empty
        cells=[c for c in body['cells'] if isinstance(c,dict) and (canonical_chain_id(c.get('chain')) or str(c.get('chain')))==wanted
               and (wallet is None or c.get('wallet')==wallet)]
        # No artifact may assert a supported strategy: this reader only exposes
        # coverage. Future conclusion publication has a separate validated receipt.
        cells=[{**c,'chain':str(chain),'strategy':None,'status':'blocked'} for c in cells]
        studies=[s for s in body.get('studies',[]) if isinstance(s,dict) and str(s.get('chain'))==wanted
                 and (wallet is None or s.get('wallet')==wallet)]
        return {**body,'cells':cells,'studies':studies,'state':'unavailable' if body.get('source_status')!='ok' else 'stale' if age>max_age_s else 'ok',
                'age_s':max(0.,age),'publication':'diagnostic_only','funded_authority':False}
    except (OSError,ValueError,TypeError,KeyError,AttributeError): return empty


def read_dossier(path, *, wallet, chain, now):
    """30-day evidence view; unavailable receipts never inherit legacy paper PnL."""
    from datetime import datetime
    from collections import Counter
    from sniper.research.wallet_execution.evidence import episode_gate, dossier_admission_gate
    from sniper.research.wallet_execution.replay import replay
    end = datetime.fromisoformat(now).timestamp(); start = end - 30 * 86400
    view = {'protocol': 'wallet-fast-move-v1', 'wallet': wallet, 'chain': chain,
            'generated_at': now, 'window_days': 30, 'state': 'unavailable', 'funnel': None,
            'strategy_supported': False, 'entries': [], 'notes': [], 'baseline': None,
            'rules': ['Recorded paid source buy and complete wallet/mint zero pre-buy holdings transcript. Add-ons excluded.',
                      'Decision-time market cap below $1M; unknown stays unknown.',
                      'Every frozen default admission gate passes before entry; no current-setting backfill.',
                      'Independent delayed entry; exact whole-position exit floors, full costs and default hold window.',
                      '5/10/15/30/60/120/300-second paths describe opportunity; peak is not realized profit.',
                      'All observed buys remain in the funnel. Missing paths and costs remain censored.',
                      'Buyer flow is association; it does not prove this wallet caused the move.']}
    view['rules'].append('Receipt checks are structural. Producer provenance and gate evaluations require independent review.')
    try:
        with closing(get_readonly_connection(str(path))) as db:
            db.execute('PRAGMA busy_timeout=100')
            db.execute('BEGIN')  # Own research snapshot only; closing ends the bounded read.
            data = store.dossier_rows(db, wallet=wallet, chain=chain, start=start, end=end)
            baseline = data['baseline']; view.update(baseline=baseline, notes=data['notes'])
            valid = 0; modeled = 0; reasons = Counter(); evaluated = 0; limited = False
            # ponytail: at most 200 origins and ten full paths per read; larger cohorts need worker snapshots.
            for row in data['rows']:
                codes = []
                receipt = json.loads(row['receipt']) if row['receipt'] else {}
                gate = json.loads(row['eligibility']) if row['eligibility'] else {}
                if not isinstance(receipt, dict): receipt = {}
                if not isinstance(gate, dict): gate = {}
                origin = {'origin_id': row['origin_id'], 'wallet': wallet, 'chain': chain,
                          'mint': row['mint'], 'observed_at': row['observed_at']}
                admission = dossier_admission_gate(receipt, baseline, origin, now=end)
                codes.extend(admission.codes)
                body = receipt.get('evidence') or {}
                decision = body.get('decision_at') if isinstance(body, dict) else None
                admission_valid = not codes
                if not gate.get('eligible'): codes.extend(gate.get('codes') or ['execution_path_missing'])
                paper = None; fast = None
                if not codes and evaluated >= 10:
                    codes.append('evaluation_page_limit'); limited = True
                if not codes:
                    evaluated += 1
                    episode = store.dossier_episode(db, row['origin_id'])
                    if episode is None: codes.append('episode_body_unavailable_or_oversize')
                    elif not episode_gate(episode, decision_at=decision).eligible: codes.extend(episode_gate(episode, decision_at=decision).codes)
                    elif (episode['window']['end'] > end
                          or any(f['at'] > end or f['available_at'] > end for f in episode['frames'])):
                        codes.append('path_after_snapshot')
                    else:
                        if (any(episode.get(k) != origin[k] for k in ('origin_id', 'wallet', 'chain', 'mint'))
                            or any(episode['source'].get(k) != body['source'].get(k) for k in
                                   ('kind', 'identity_status', 'address', 'transaction', 'paid_atomic', 'acquired_atomic',
                                    'decimals', 'spent_usd', 'source_at', 'available_at', 'received_at', 'order', 'proof'))):
                            codes.append('episode_admission_identity_mismatch')
                        else:
                            paper = replay(episode, baseline['policy'], decision_at=decision)
                        if paper is not None:
                            if paper['state'] not in ('closed', 'unsellable'): codes.append(paper['reason'])
                            else: fast = dossier_fast_move(episode, paper)
                state = 'valid' if admission_valid else 'excluded_or_unknown'
                valid += admission_valid; modeled += not codes; reasons.update(set(codes))
                view['entries'].append({'origin_id': row['origin_id'], 'mint': row['mint'],
                    'observed_at': row['observed_at'], 'state': state, 'reasons': codes,
                    'admission': {'decision_at': decision, 'digest': receipt.get('admission_digest'),
                                  'validation': 'structural_only', 'external_provenance_review': 'required'},
                    'paper': paper, 'fast_move': fast, 'measurement_state': 'censored_or_pending' if codes else 'modeled'})
            observed, coins, _, _ = data['counts']
            complete = observed <= len(data['rows']) and not limited
            run = json.loads(data['run']['payload']) if data['run'] else None
            last_run = datetime.fromisoformat(data['run']['at']).timestamp() if data['run'] else None
            collection_start = data['first_run']
            if baseline and collection_start is not None:
                collection_start = max(collection_start, datetime.fromisoformat(baseline['sealed_at']).timestamp())
            view.update(state='ok', funnel={'observed_buys': observed, 'distinct_coins': coins,
                        'valid_entries': valid if complete else None,
                        'excluded_or_unknown': observed - valid if complete else None,
                        'modeled_outcomes': modeled if complete else None,
                        'pending_or_censored': valid - modeled if complete else None},
                        reasons=dict(reasons), truncated=not complete,
                        coverage={'days_observed': min(30, max(0, (end - max(start, collection_start or end)) / 86400)),
                                  'full_30_days_proven': False, 'unplaced_buy_clocks': data['clock_unknown'], 'last_collection_at': data['run']['at'] if data['run'] else None,
                                  'source_state': run.get('source_status') if run else 'not_collected',
                                  'stale': last_run is None or not 0 <= end-last_run <= 120,
                                  'gaps': run.get('continuity_gaps', []) if run else ['collection_start_unknown']})
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        view.update(state='unavailable', funnel=None, entries=[], reason=f'{type(exc).__name__}: {exc}')
    return view


def dossier_fast_move(episode, result):
    """Descriptive quote-floor envelope after modeled entry; never an exit policy."""
    entry = next(f for f in episode['frames'] if f['sequence'] == result['entry_sequence']
                 and f['at'] == result['entry_quote_at'])
    # The modeled clip's own acquisition cost, never the frame's primary quote: an alternative clip has
    # its own spend and fee (Codex W3: +35.35% shown where the replay's own cash said +1.70%).
    quantity = result['quantity_atomic']
    if not (number(result.get('entry_spend_usd'), positive=True) and number(result.get('entry_fee_usd'))):
        return {str(h): {'state': 'censored'} for h in (5, 10, 15, 30, 60, 120, 300)}
    cost = result['entry_spend_usd'] + result['entry_fee_usd']
    path = []
    for frame in episode['frames']:
        if (frame['sequence'] < entry['sequence'] or frame['at'] < entry['at']
            or frame['available_at'] < result['entry_at']): continue
        quote = frame['sells'].get(quantity)
        gain = ((quote['floor_usd'] - quote['fee_usd']) / cost - 1) * 100 if quote and quote.get('status', 'ok') == 'ok' else None
        path.append((frame['available_at'] - result['entry_at'], gain))
    out = {}
    for horizon in (5, 10, 15, 30, 60, 120, 300):
        points = [gain for elapsed, gain in path if 0 <= elapsed <= horizon]
        end = next(((elapsed, gain) for elapsed, gain in path if elapsed >= horizon), None)
        if not points or end is None or end[0] > horizon + 1 or any(p is None for p in points) or end[1] is None:
            out[str(horizon)] = {'state': 'censored'}; continue
        peak = max(points)
        out[str(horizon)] = {'state': 'measured', 'peak_pct': peak, 'endpoint_pct': end[1],
                             'worst_pct': min(points), 'retrace_pp': peak - end[1]}
    return out

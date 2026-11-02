#!/usr/bin/env python3
"""Bounded armed-wallet links from saved windows and separate, paced research reads."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import statistics
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sniper.persistence.db_core import get_readonly_connection
from sniper.persistence.db_wallet_identity import read_account_claims
from sniper.services.wallet_identity import _account, _claims, verified_sol_funding
from sniper.services.wallet_link_proofs import evm_buy, evm_funding, sol_cached_buys
from sniper.services.wallet_universe_metrics import aware, assets_for
from scripts.research.wallet_universe_metrics import endpoints, research_reader, verified_chain, flat

METHOD = 'armed-wallet-links-v1'
CHAINS = ('solana', '8453', '4663', '56', '1', '5042', '57073')
EXPLORERS = {'8453':'https://base.blockscout.com', '1':'https://eth.blockscout.com',
             '4663':'https://robinhoodchain.blockscout.com'}
STOP = {'budget', 'funded_book_open', 'rate_limit_pause', 'rate_limit', 'credits_exhausted'}


def armed_pairs(ledger, claims, *, now_iso=None):
    groups = _claims(claims); rows = []
    for uid, chains in ledger.execute('SELECT source_user_id,real_chains FROM memecoin_wallets '
            'WHERE real_approved=1 AND paused=0 AND excluded=0 ORDER BY source_user_id'):
        for chain in sorted(set(str(chains or '').split(',')) & set(CHAINS)):
            verified, _conflicts = _account(groups, uid, chain)
            addresses = sorted(r['address'] for r in verified)
            rows.append(dict(uid=uid, chain=chain, main_addresses=addresses, statistics=[], coverage=dict(
                state='unavailable', source_units=0, sampled_coins=0, complete_coins=0, unknown_coins=0,
                funding_state='unavailable', funding_candidates=0, funding_verified=0,
                reason='no_source_windows' if addresses else 'verified_address_unavailable',
                window_from=(aware(now_iso) - timedelta(days=14)).isoformat() if now_iso else None,
                window_to=now_iso, capped=[])))
    return rows


class Captures:
    """Immutable successful read captures; failures are separate and retryable next run."""
    def __init__(self, path, chain, rpc=None):
        self.path, self.chain, self.rpc = path / chain, chain, rpc
        self.path.mkdir(parents=True, exist_ok=True)
        self.failures = []
        self.phase_limit = None
        self.disabled = {}
        usage=self.path/'rpc_usage.json'
        self.prior_used=json.loads(usage.read_text()).get('used',0) if usage.exists() else 0
        self.start_used=rpc.used if rpc else 0
        self.run_id=uuid.uuid4().hex

    def __call__(self, method, params):
        key = hashlib.sha256(json.dumps([method, params], sort_keys=True).encode()).hexdigest()
        mutable=(method in {'eth_blockNumber','eth_chainId','getGenesisHash','getTransactionsForAddress','blockscout_address_transactions'}
                 or method=='eth_call' and params[-1] in ('latest','pending'))
        path = self.path / (key + ('.'+self.run_id if mutable and self.rpc else '') + '.json')
        if mutable and self.rpc is None:
            saved=[p for p in self.path.glob(key+'*.json') if not p.name.endswith('.failed.json')]
            if saved:path=max(saved,key=lambda p:p.stat().st_mtime_ns)
        if path.exists():
            capture = json.loads(path.read_text())
            if capture.get('chain') != self.chain or capture.get('method') != method or capture.get('params') != params:
                raise ValueError('capture_identity_mismatch')
            return capture['result']
        if self.rpc is None: raise ValueError('research_provider_unavailable')
        if method in self.disabled:raise ValueError(self.disabled[method])
        if self.phase_limit is not None and self.rpc.used>=self.phase_limit:raise ValueError('funding_budget')
        try:
            if method=='blockscout_address_transactions': result=self._explorer(params)
            else: result = self.rpc(method, params)
        except ValueError as exc:
            self.failures.append(dict(method=method, params=params, reason=str(exc)))
            if ((method=='blockscout_address_transactions' and str(exc)=='explorer_rejected')
                    or sum(r['method']==method and r['reason']==str(exc) for r in self.failures)>=3):
                self.disabled[method]=str(exc)
            return self._failure(path, method, params, exc)
        finally:
            self._save(self.path/'rpc_usage.json',dict(used=self.prior_used+self.rpc.used-self.start_used))
        self._save(path, dict(chain=self.chain, method=method, params=params, result=result,
                             observed_at=datetime.now(timezone.utc).isoformat(),run_id=self.run_id))
        return result

    def _explorer(self, params):
        """Documented Blockscout v2 discovery only; every resulting hash is proved by chain RPC."""
        import requests
        if self.rpc.used>=self.rpc.limit:raise ValueError('budget')
        self.rpc.pace();self.rpc.used+=1
        try:
            response=requests.get(EXPLORERS[self.chain]+'/api/v2/addresses/'+params[0]+'/transactions',
                                  params={'filter':'to'},timeout=12,allow_redirects=False)
        except requests.Timeout:raise ValueError('explorer_timeout') from None
        except requests.RequestException:raise ValueError('explorer_transport') from None
        if response.status_code in (401,403):raise ValueError('explorer_rejected')
        if response.status_code==429:raise ValueError('rate_limit')
        if response.status_code>=400 or 300<=response.status_code<400:raise ValueError('explorer_http_error')
        try: data=response.json()
        except ValueError:raise ValueError('malformed_explorer_response') from None
        if not isinstance(data,dict) or not isinstance(data.get('items'),list) or 'next_page_params' not in data:
            raise ValueError('malformed_explorer_page')
        return data

    def _failure(self, path, method, params, exc):
        self._save(path.with_suffix('.failed.json'), dict(chain=self.chain, method=method, params=params, reason=str(exc)))
        raise exc

    @staticmethod
    def _save(path, value):
        tmp = path.with_suffix('.tmp'); tmp.write_text(json.dumps(value, allow_nan=False)); tmp.replace(path)


def _bundle(read, signature):
    receipt = read('eth_getTransactionReceipt', [signature])
    tx = read('eth_getTransactionByHash', [signature])
    if receipt.get('transactionHash','').lower()!=signature.lower() or tx.get('hash','').lower()!=signature.lower():
        raise ValueError('requested_transaction_mismatch')
    header = read('eth_getBlockByNumber', [receipt['blockNumber'], False])
    return receipt, tx, header


def _sol_facts(tx, now_iso):
    rows = verified_sol_funding(tx, now_iso)
    return [dict(r, chain='solana', amount=r['amount_sol'], asset='native', symbol='SOL', decimals=9) for r in rows]


def funding_bundle(read,signature,assets):
    """Only net directed funding candidates spend an EOA read; protocol payments remain unsupported."""
    from sniper.services.wallet_link_proofs import _mined
    from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC
    receipt,transaction,header=_bundle(read,signature)
    sender,target,_sig,_order,_at=_mined(receipt,transaction,header,read.chain)
    credit,forward=defaultdict(int),defaultdict(int)
    for log in receipt['logs']:
        topics=log['topics'];asset=log['address'].lower()
        if asset not in assets or len(topics)!=3 or topics[0].lower()!=TRANSFER_TOPIC:continue
        src,dst='0x'+topics[1][-40:].lower(),'0x'+topics[2][-40:].lower();amount=int(log['data'],16)
        credit[(asset,src)]-=amount;credit[(asset,dst)]+=amount
        if src==sender and src!=dst:forward[(asset,dst)]+=amount
    destinations={dst for (asset,dst),amount in forward.items() if amount and credit[(asset,dst)]>=amount}
    codes={dst:read('eth_getCode',[dst,receipt['blockNumber']]) for dst in sorted(destinations)[:2]}
    code=(read('eth_getCode',[target,receipt['blockNumber']])
          if int(transaction.get('value','0x0'),16)>0 and transaction.get('input')=='0x' else None)
    return evm_funding(receipt,transaction,header,chain=read.chain,assets=assets,
                       recipient_code=code,recipient_codes=codes)


def retained_funding(read,assets,now_iso):
    """Revalidate successful raw receipts even when a later history read is unavailable."""
    offline=Captures(read.path.parent,read.chain)
    try:verified_chain(read.chain,offline)
    except ValueError:return []
    if read.chain!='solana':assets=dict(_evm_assets(offline,read.chain),**assets)
    found=[];now=aware(now_iso).timestamp()
    for path in read.path.glob('*.json'):
        if path.name.endswith('.failed.json') or path.name=='rpc_usage.json':continue
        try:
            raw=json.loads(path.read_text())
            if 'result' not in raw or raw.get('chain')!=read.chain:continue
            if read.chain=='solana' and raw.get('method')=='getTransaction':
                if raw['result']['transaction']['signatures'][0]!=raw['params'][0]:continue
                found.extend(_sol_facts(raw['result'],now_iso))
            elif read.chain!='solana' and raw.get('method')=='eth_getTransactionReceipt':
                found.extend(r for r in funding_bundle(offline,raw['params'][0],assets) if r['funded_at']<=now)
        except (ValueError,KeyError,TypeError,AttributeError,IndexError):continue
    return found


def _local_windows(root, pairs, now_iso):
    """Decode one saved history at a time; release the read snapshot before provider work."""
    from sniper.services.wallet_universe_flow import METHOD as FLOW_METHOD
    selected = {(p['uid'], p['chain']): p for p in pairs}
    buys, evm_candidates = [], defaultdict(list)
    complete, sampled, source_coins = defaultdict(set), defaultdict(set), defaultdict(set)
    path = root / 'data/wallet_universe_metrics.db'
    if not path.exists(): return buys, evm_candidates
    since, now = (aware(now_iso) - timedelta(days=14)).timestamp(), aware(now_iso).timestamp()
    conn = get_readonly_connection(str(path))
    try:
        for raw_unit, raw_result in conn.execute('SELECT unit,result FROM flow_units WHERE result IS NOT NULL'):
            try:
                unit, result = json.loads(raw_unit), json.loads(raw_result)
                key = unit['uid'], unit['chain']; pair = selected.get(key)
                if not pair or type(unit.get('t0')) is not int or not since <= unit['t0'] <= now: continue
                pair['coverage']['source_units'] += 1
                source_coins[key].add(unit['mint'])
                state = result.get('state') or {}; identity = (state.get('binding') or {}).get('identity') or {}
                source = state.get('source')
                if (unit['address'] not in pair['main_addresses'] or result.get('method') != FLOW_METHOD
                        or state.get('version') != FLOW_METHOD or not source or source.get('signature') != unit['signature']
                        or source.get('t0') != unit['t0']
                        or any(identity.get(k) != unit.get(k) for k in ('chain','uid','address','mint','signature','t0'))): continue
                sampled[key].add(unit['mint'])
                if unit['chain'] == 'solana':
                    facts=sol_cached_buys(unit, result); buys.extend(facts)
                    positive={r['signature'] for r in state.get('records',[]) if any(int(l['amount'])>0 for l in r.get('legs',[]))}
                    proved={r['buy_signature'] for r in facts}
                    if (result.get('status') == 'complete' and result.get('before_complete') is True
                            and result.get('after_complete') is True and positive<=proved): complete[key].add(unit['mint'])
                else:
                    pair['coverage']['capped']=['evm_buyer_receipts']
                    for rec in state.get('records', []):
                        if any(int(leg['amount']) > 0 for leg in rec.get('legs', [])):
                            evm_candidates[key].append(dict(unit=unit, source=source, record=rec))
            except (ValueError, TypeError, KeyError, AttributeError): continue
    finally: conn.close()
    for key, pair in selected.items():
        count, census, done = len(sampled[key]),len(source_coins[key]),len(complete[key]); coverage=pair['coverage']
        coverage.update(source_coins=census,sampled_coins=count,complete_coins=done,unknown_coins=census-done)
        if pair['main_addresses'] and census:
            coverage.update(state='ok' if done == census else 'partial',reason=None if done == census else 'incomplete_source_windows')
    return buys, evm_candidates


def _funding_candidates(root, pairs):
    mains = {a for p in pairs if p['chain'] == 'solana' for a in p['main_addresses']}
    path = root / 'data/memecoin_wallet_graph.db'; candidates = defaultdict(list)
    if not path.exists(): return candidates
    conn = get_readonly_connection(str(path))
    try:
        for wallet, signature in conn.execute("SELECT wallet,funding_sig FROM memecoin_wallet_funders WHERE status='resolved' AND funding_sig IS NOT NULL"):
            if wallet in mains: candidates[wallet].append(signature)
    finally: conn.close()
    return candidates


def _sol_discover(read, address):
    opts = dict(transactionDetails='full', sortOrder='asc', limit=20, encoding='json',
                maxSupportedTransactionVersion=1, filters=dict(status='succeeded'))
    page = read('getTransactionsForAddress', [address, opts])
    if not isinstance(page, dict) or not isinstance(page.get('data'), list) or 'paginationToken' not in page:
        raise ValueError('malformed_history_page')
    signatures = []
    for tx in page['data']:
        try:
            keys = tx['transaction']['message']['accountKeys']; index = keys.index(address)
            if tx['meta']['postBalances'][index] > tx['meta']['preBalances'][index]:
                signatures.append(tx['transaction']['signatures'][0])
        except (KeyError, ValueError, IndexError, TypeError): continue
    return signatures[:2]


def _evm_assets(read, chain):
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    assets = {}
    quote = CHAIN_QUOTE.get(chain) or {}
    for asset, symbol, decimals in assets_for(chain):
        if asset == str(quote.get('token', '')).lower(): decimals = quote['decimals']
        elif decimals is None:
            try: decimals = int(read('eth_call', [dict(to=asset, data='0x313ce567'), 'latest']), 16)
            except (ValueError, TypeError): continue
        if type(decimals) is int and 0 <= decimals <= 36: assets[asset] = (symbol, decimals)
    return assets


def _evm_discover(read, address, assets, head):
    from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC
    topic = '0x' + '0' * 24 + address[2:]
    signatures = []
    if read.chain in EXPLORERS:
        try:
            page=read('blockscout_address_transactions',[address])
            for tx in page['items']:
                if (str((tx.get('to') or {}).get('hash','')).lower()==address
                        and str(tx.get('value','')).isdigit() and int(tx['value'])>0):
                    signatures.append(tx['hash'])
        except ValueError as exc:
            if str(exc) in STOP:raise
    for topics in ([TRANSFER_TOPIC, topic], [TRANSFER_TOPIC, None, topic]):
        logs = read('eth_getLogs', [dict(address=list(assets), fromBlock=hex(max(0,head-399)),
                                       toBlock=hex(head), topics=topics)])
        if not isinstance(logs, list): raise ValueError('malformed_funding_logs')
        for log in logs:
            signature = log.get('transactionHash')
            if signature not in signatures: signatures.append(signature)
    return signatures[:1]


def _fair(rows):
    """One candidate per pair per round, rather than spending the budget on one busy coin."""
    pending = [iter(rows[key]) for key in sorted(rows)]
    while pending:
        remaining = []
        for stream in pending:
            value = next(stream, None)
            if value is not None: yield value; remaining.append(stream)
        pending = remaining


def without_custody(buys,funding,own):
    """Current custody overrides stale capture exclusions; roster peers remain eligible."""
    return ([r for r in buys if r['address'] not in own],
            [r for r in funding if r['funder'] not in own and r['recipient'] not in own])


def funding_phase_limit(rpc):
    """Reserve half the remaining calls for buyer receipts, including a resumed budget."""
    return rpc.used+max(0,(rpc.limit-rpc.used)//2)


def funding_hubs(root,funding):
    """Preserve the Solana graph's cached degree lower bound; it does not identify a service."""
    path=root/'data/memecoin_wallet_graph.db'
    if not path.exists():return
    conn=get_readonly_connection(str(path))
    try:degrees=dict(conn.execute('SELECT funder,COUNT(DISTINCT wallet) FROM memecoin_wallet_funders '
                                 'WHERE funder IS NOT NULL GROUP BY funder'))
    finally:conn.close()
    for fact in funding:
        degree=degrees.get(fact['funder'],0) if fact['chain']=='solana' else 0
        if degree>3:fact.update(hub=True,funder_degree_lower_bound=degree,
                               hub_source='wallet_graph_cached_funding_degree_lower_bound')


def refresh_snapshot(previous,current,buys,funding):
    """Rebind publication to the current armed roster; changed claims keep activity unknown."""
    old={(p['uid'],p['chain']):p for p in previous}
    pairs=[];stable={};mains=defaultdict(set)
    for pair in current:
        key=pair['uid'],pair['chain'];prior=old.get(key)
        mains[pair['chain']].update(pair['main_addresses'])
        if prior and prior['main_addresses']==pair['main_addresses']:
            pairs.append(prior);stable[key]=set(pair['main_addresses'])
        else:
            pair['coverage'].update(state='unavailable',reason='roster_changed_during_collection')
            pairs.append(pair)
    return (pairs,[r for r in buys if r['main_address'] in stable.get((r['uid'],r['chain']),set())],
            [r for r in funding if mains[r['chain']].intersection((r['funder'],r['recipient']))])


def cap_links(artifact):
    """Retain full statistics and fairly crop examples to the consumer's exact JSON limits."""
    from sniper.services.wallet_identity_links import MAX_BYTES
    encode=lambda value:json.dumps(value,allow_nan=False).encode('utf-8')
    if len(artifact['buys'])<=12000 and len(encode(artifact))<=MAX_BYTES:return artifact
    rows=artifact['buys'];artifact['buys']=[]
    counts=defaultdict(int);grouped=defaultdict(lambda:defaultdict(list))
    for row in rows:
        key=row['uid'],row['chain'];counts[key]+=1;grouped[key][row['address']].append(row)
    pairs={(p['uid'],p['chain']):p for p in artifact['pairs']}
    for key,count in counts.items():
        coverage=pairs[key]['coverage'];coverage['evidence_omitted']=coverage.get('evidence_omitted',0)+count
        if 'artifact_evidence' not in coverage['capped']:coverage['capped'].append('artifact_evidence')
    size=len(encode(artifact))
    if size>MAX_BYTES:raise ValueError('identity_links_metadata_oversize')
    fair={key:list(_fair(peers)) for key,peers in grouped.items()}
    retained=defaultdict(int)
    for row in _fair(fair):
        if len(artifact['buys'])>=12000:break
        additional=len(encode(row))+(2 if artifact['buys'] else 0)
        if size+additional>MAX_BYTES:continue
        artifact['buys'].append(row);size+=additional;retained[(row['uid'],row['chain'])]+=1
    for key,count in counts.items():
        coverage=pairs[key]['coverage'];coverage['evidence_omitted']-=retained[key]
        if count==retained[key]:coverage['capped'].remove('artifact_evidence')
    return artifact


def publish_links(path,artifact,*,now_iso):
    """Validate before atomic replacement; rejected output cannot erase the previous artifact."""
    from sniper.services.wallet_identity_links import MAX_BYTES,_validate
    from scripts.research.wallet_follow_funnel import publish_universe
    cap_links(artifact)
    _validate(artifact,aware(now_iso).timestamp())
    if len(json.dumps(artifact,allow_nan=False).encode('utf-8'))>MAX_BYTES:raise ValueError('oversize')
    publish_universe(path,artifact)


def _summarize(pairs, buys, funding):
    grouped = defaultdict(list)
    for row in buys: grouped[(row['uid'], row['chain'], row['address'])].append(row)
    selected_rows = []
    for pair in pairs:
        uid, chain = pair['uid'], pair['chain']; main = set(pair['main_addresses'])
        funded = {r['recipient'] if r['funder'] in main else r['funder'] for r in funding
                  if r['chain'] == chain and main.intersection((r['funder'],r['recipient']))}
        stats = []
        for (u, c, peer), facts in grouped.items():
            if (u,c) != (uid,chain): continue
            before = {r['mint'] for r in facts if tuple(r['order']) < tuple(r['source_order'])}
            after = {r['mint'] for r in facts if tuple(r['order']) > tuple(r['source_order'])}
            coins = {r['mint'] for r in facts}; first={}
            for fact in facts:
                if tuple(fact['order'])<tuple(fact['source_order']):
                    old=first.get(fact['mint'])
                    if old is None or tuple(fact['order'])<tuple(old['order']):first[fact['mint']]=fact
            lead = [r['source_t0']-r['t0'] for r in first.values()]
            stats.append(dict(address=peer,n_coins=len(coins),before_coins=len(before),after_coins=len(after),
                days=len({min(r['source_t0'] for r in facts if r['mint']==coin)//86400 for coin in coins}),
                lead_p50_s=statistics.median(lead) if lead else None,
                lead_p90_s=sorted(lead)[min(len(lead)-1,int(len(lead)*.9))] if lead else None))
        stats.sort(key=lambda r:(r['address'] not in funded, -r['n_coins'], -r['before_coins'], r['address']))
        pair['statistics'] = stats[:20]; pair['coverage']['relations_omitted'] = max(0,len(stats)-20)
        if len(stats)>20: pair['coverage']['capped'].append('buyer_relations')
        omitted = 0
        for stat in stats[:20]:
            rows = grouped[(uid,chain,stat['address'])]
            unique = {(r['mint'],r['buy_signature'],r['source_signature']):r for r in rows}
            rows = sorted(unique.values(),key=lambda r:(tuple(r['order'])>=tuple(r['source_order']),-r['source_t0'],r['mint']))
            selected_rows.extend(rows[:10]); omitted += max(0,len(rows)-10)
        pair['coverage']['evidence_omitted'] = omitted
        if omitted: pair['coverage']['capped'].append('buyer_evidence')
        pair['coverage']['funding_verified'] = sum(r['chain']==chain and bool(main.intersection((r['funder'],r['recipient']))) for r in funding)
    return selected_rows


def collect(root, outdir, *, now_iso, budget=160, offline=False):
    if not 0 <= budget <= 160: raise ValueError('research_budget_must_be_at_most_160_per_chain')
    outdir.mkdir(parents=True,exist_ok=True)
    ledger = get_readonly_connection(str(root/'sniper_tracker.db'))
    try: pairs = armed_pairs(ledger,read_account_claims(ledger),now_iso=now_iso)
    finally: ledger.close()
    buys, evm_candidates = _local_windows(root,pairs,now_iso)
    funding, funders = [], _funding_candidates(root,pairs)
    # Existing durable proofs are never overwritten by the rolling research run.
    old = root/'data/wallet_funding_receipts.json'
    if old.exists():
        try:
            for tx in json.loads(old.read_text()).get('transactions',[]): funding.extend(_sol_facts(tx,now_iso))
        except (ValueError, KeyError, TypeError): pass
    import os
    from sniper.services.memecoin_wallet_map import own_address_set
    own=own_address_set(os.environ.get('MEMECOIN_OWN_ADDRESSES',''))
    urls = {} if offline else endpoints(os.environ); reads = {}; report = {}
    try:
        for chain in CHAINS:
            usage=outdir/'captures'/chain/'rpc_usage.json'
            prior=json.loads(usage.read_text()).get('used',0) if usage.exists() else 0
            if type(prior) is not int or prior<0:raise ValueError('invalid_research_usage')
            remaining=max(0,budget-prior)
            rpc = research_reader(root,chain,urls[chain],remaining) if chain in urls and remaining else None
            reads[chain] = read = Captures(outdir/'captures',chain,rpc)
            chain_pairs = [p for p in pairs if p['chain']==chain and p['main_addresses']]
            has_buyers=any(c==chain and bool(rows) for (_uid,c),rows in evm_candidates.items())
            assets, head, stopped = {}, None, None
            if rpc or any(read.path.glob('*.json')):
                try:
                    verified_chain(chain,read)
                    if chain!='solana': assets=_evm_assets(read,chain); head=int(read('eth_blockNumber',[]),16)
                except ValueError as exc: stopped=str(exc)
            elif not offline: stopped='research_provider_unavailable'
            funding.extend(retained_funding(read,assets,now_iso))
            if rpc and chain!='solana' and has_buyers:read.phase_limit=funding_phase_limit(rpc)
            for pair in chain_pairs:
                coverage=pair['coverage']; coverage['funding_state']='partial'
                coverage['capped'].append('funding_history')
                for address in pair['main_addresses']:
                    if stopped: coverage['funding_state']=stopped; break
                    try:
                        candidates=list(funders.get(address,[])) if chain=='solana' else []
                        if not candidates:
                            candidates=_sol_discover(read,address) if chain=='solana' else _evm_discover(read,address,assets,head) if assets and head else []
                        coverage['funding_candidates']+=len(candidates)
                        for signature in candidates:
                            if chain=='solana':
                                tx=read('getTransaction',[signature,dict(encoding='jsonParsed',commitment='confirmed',maxSupportedTransactionVersion=1)])
                                if tx['transaction']['signatures'][0]!=signature:raise ValueError('requested_transaction_mismatch')
                                facts=_sol_facts(tx,now_iso)
                            else:facts=funding_bundle(read,signature,assets)
                            funding.extend(r for r in facts if address in (r['funder'],r['recipient']))
                    except (ValueError,KeyError,TypeError) as exc:
                        reason=str(exc) if isinstance(exc,ValueError) else 'malformed_funding_receipt'
                        coverage['funding_state']=reason
                        if reason in STOP or reason=='funding_budget': stopped=reason; break
            read.phase_limit=None
            if stopped=='funding_budget':stopped=None
            if chain!='solana' and not stopped and assets:
                # shortcut: four coins/four candidates each per pair; increase only after provider capacity is measured.
                fair_candidates={}
                for pair in chain_pairs:
                    key=pair['uid'],chain; per_coin=defaultdict(list)
                    for candidate in evm_candidates[key]:
                        mint=candidate['unit']['mint']
                        if candidate['record']['signature'] not in {r['record']['signature'] for r in per_coin[mint]}:
                            per_coin[mint].append(candidate)
                    fair_candidates[key]=list(_fair({mint:per_coin[mint][:4] for mint in sorted(per_coin)[:4]}))
                    if evm_candidates[key]: pair['coverage']['capped'].append('evm_buyer_receipts')
                for candidate in _fair(fair_candidates):
                    unit,source,record=candidate['unit'],candidate['source'],candidate['record']
                    try:
                        fact=evm_buy(*_bundle(read,record['signature']),chain=chain,mint=unit['mint'],quotes=list(assets))
                        if not fact or fact['address'] in unit.get('proof_addresses',[]) or fact['address']==unit['address']:continue
                        if fact['order']!=record['order'] or not unit['t0']-60<=fact['t0']<=unit['t0']+60:continue
                        buys.append(dict(fact,uid=unit['uid'],chain=chain,main_address=unit['address'],mint=unit['mint'],
                            source_signature=unit['signature'],source_order=source['order'],source_t0=unit['t0']))
                    except (ValueError,KeyError,TypeError) as exc:
                        if str(exc) in STOP: stopped=str(exc);break
            report[chain]=dict(rpc_calls=rpc.used if rpc else 0,total_rpc_calls=prior+(rpc.used if rpc else 0),
                              stop=stopped,failures=len(read.failures))
        funding=list({(r['chain'],r['funding_signature'],r['funder'],r['recipient'],r['asset']):r for r in funding}.values())
        buys,funding=without_custody(buys,funding,own)
        ledger=get_readonly_connection(str(root/'sniper_tracker.db'))
        try:current=armed_pairs(ledger,read_account_claims(ledger),now_iso=now_iso)
        finally:ledger.close()
        pairs,buys,funding=refresh_snapshot(pairs,current,buys,funding)
        funding_hubs(root,funding)
        Captures._save(outdir/'all-buyer-facts.json',dict(version=1,generated_at=now_iso,buys=buys))
        cropped=_summarize(pairs,buys,funding)
        artifact=dict(version=1,method=METHOD,generated_at=now_iso,pairs=pairs,buys=cropped,funding=funding)
        publish_links(outdir/'wallet_identity_links.json',artifact,now_iso=now_iso)
        report.update(pairs=len(pairs),funding_verified=len(funding),buyer_facts=len(buys),saved_buys=len(artifact['buys']),
                      output=str(outdir/'wallet_identity_links.json'))
        Captures._save(outdir/'report.json',report)
        return report
    finally:
        for read in reads.values():
            if read.rpc:read.rpc.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--outdir',type=Path,required=True)
    parser.add_argument('--budget',type=int,default=160)
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args(argv)
    from sniper.util.envfiles import load_env_files
    from sniper.util.clock import real_utc_now_iso
    load_env_files(str(args.root),names=('.env',))
    print(json.dumps(collect(args.root,args.outdir,now_iso=real_utc_now_iso(path=args.root/'data/clock_offset.json'),
                             budget=args.budget,offline=args.offline)))
    return 0


if __name__=='__main__':raise SystemExit(main())

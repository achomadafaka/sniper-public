"""Data-only raw confirmed tail. No provider, custody or native callback ownership."""
from __future__ import annotations

import json
import hashlib
import copy
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import base58

from sniper.persistence import db_memecoin as db
from sniper.services import memecoin_known_group_canary as policy
from sniper.settings.known_group import KNOWN_GROUP_TAIL_PAGE,KNOWN_GROUP_TAIL_PENDING_MAX
from sniper.util.clock import real_utc_now_iso

logger = logging.getLogger(__name__)


def _wire_field(raw, number, expected_wire):
    """Preserve scalar presence lost by this host's proto3 generated index field."""
    offset,found = 0,None
    def varint():
        nonlocal offset
        value = 0
        for shift in range(0,70,7):
            if offset>=len(raw):
                raise ValueError('known_group_raw_wire_truncated')
            byte = raw[offset]
            offset += 1
            value |= (byte&127)<<shift
            if byte<128:
                if value>=2**64:
                    break
                return value
        raise ValueError('known_group_raw_wire_invalid')
    while offset<len(raw):
        tag = varint()
        field,wire = tag>>3,tag&7
        if field==0:
            raise ValueError('known_group_raw_wire_invalid')
        if wire==0:
            value = varint()
        elif wire==2:
            size = varint()
            value = raw[offset:offset+size]
            offset += size
        elif wire in (1,5):
            size = 8 if wire==1 else 4
            value = raw[offset:offset+size]
            offset += size
        else:
            raise ValueError('known_group_raw_wire_invalid')
        if offset>len(raw):
            raise ValueError('known_group_raw_wire_truncated')
        if field==number:
            if wire!=expected_wire or found is not None:
                raise ValueError('known_group_raw_wire_ambiguous')
            found = value
    return found


def _explicit_index(info, raw=None):
    if raw is not None:
        envelope = _wire_field(raw,4,2)
        metadata = _wire_field(envelope,1,2) if envelope is not None else None
        index = _wire_field(metadata,5,0) if metadata is not None else None
        if index is not None and index!=info.index:
            raise ValueError('known_group_order_mismatch')
        return index
    try:
        return int(info.index) if info.HasField('index') else None
    except ValueError:
        return int(info.index) if info.index > 0 else None


def _compiled_profile(update, parsed):
    """Keep original wire instruction indices for the venue decoder; parsed proofs keep their own shape."""
    info = update.transaction.transaction
    msg,meta = info.transaction.message,info.meta
    def instruction(ix):
        out = dict(programIdIndex=int(ix.program_id_index),accounts=list(ix.accounts),
                   data=base58.b58encode(bytes(ix.data)).decode())
        if hasattr(ix,'stack_height') and ix.HasField('stack_height'):
            out['stackHeight'] = int(ix.stack_height)
        return out
    out = dict(parsed)
    out['transaction'] = dict(signatures=parsed['transaction']['signatures'],message=dict(
        accountKeys=[base58.b58encode(bytes(k)).decode() for k in msg.account_keys],
        header=dict(numRequiredSignatures=int(msg.header.num_required_signatures),
                    numReadonlySignedAccounts=int(msg.header.num_readonly_signed_accounts),
                    numReadonlyUnsignedAccounts=int(msg.header.num_readonly_unsigned_accounts)),
        recentBlockhash=base58.b58encode(bytes(msg.recent_blockhash)).decode(),
        instructions=[instruction(ix) for ix in msg.instructions]))
    out['meta'] = dict(parsed['meta'],
        loadedAddresses=dict(writable=[base58.b58encode(bytes(k)).decode() for k in meta.loaded_writable_addresses],
                             readonly=[base58.b58encode(bytes(k)).decode() for k in meta.loaded_readonly_addresses]),
        innerInstructions=[dict(index=int(g.index),instructions=[instruction(ix) for ix in g.instructions])
                           for g in meta.inner_instructions],
        preTokenBalances=parsed['meta']['preTokenBalanceObservations'],
        postTokenBalances=parsed['meta']['postTokenBalanceObservations'])
    return out


class KnownGroupTail:
    def __init__(self, conn, *, capture_path, desk_dir, watch_path, clock=time.time, now_iso_fn=real_utc_now_iso):
        self.conn, self.capture_path = conn, Path(capture_path).resolve()
        self.desk_dir, self.watch_path = Path(desk_dir), Path(watch_path)
        self.clock, self.now_iso_fn = clock, now_iso_fn
        self.observer = db.connect_known_group_observer(self.desk_dir/'known-group-observer.db')

    def close(self):
        self.observer.close()

    def _save(self, tail, state, now):
        db.save_known_group_tail(self.observer,source=str(self.capture_path),identity=tail['identity'],
            cursor=tail['cursor'],anchor=tail['anchor'],baseline=tail['baseline'],started_at=tail['started_at'],
            last_host=now,state=state)

    def _native_profile(self, proof_ref, *, compiled=False):
        bundle = db.known_group_evidence(self.observer,proof_ref)
        if bundle.get('version') != 'known-group-native-profile-v1':
            raise ValueError('known_group_native_profile_version_unknown')
        return bundle['compiled' if compiled else 'parsed']

    def _health(self, now):
        from sniper.services.memecoin_desk_ps_asof import HEARTBEAT_MAX_AGE_S
        beat = json.loads((self.desk_dir/'universe.heartbeat').read_text())
        flow = beat['wallet_flow']
        if (beat.get('connected') is not True or not 0 <= now-policy._number(beat['ts']) <= HEARTBEAT_MAX_AGE_S
                or flow.get('enabled') is not True or flow.get('storage_initialized') is not True
                or flow.get('last_commit_state') != 'known' or type(flow.get('pending_gaps')) is not dict
                or any(reason != 'selectors_changed' for reason in flow['pending_gaps'])
                or type(flow.get('transaction_callback_errors')) is not int or flow['transaction_callback_errors'] != 0
                or type(flow.get('desired_selectors')) is not int or flow['desired_selectors'] <= 0
                or type(flow.get('submitted_selectors')) is not int or flow['submitted_selectors'] != flow['desired_selectors']
                or not 0 < policy._number(flow['submitted_at']) <= now):
            raise ValueError('known_group_source_health_unknown')
        return beat

    def _decode(self, row, tracked, now):
        from google.protobuf.message import DecodeError
        from sniper.feeds.yellowstone import geyser_pb2 as pb
        from sniper.office.wallet_stream_proof import decode_payload,confirmed_receipt
        if (row.get('commitment') != 'confirmed' or not 0 < policy._number(row['received_at']) <= now
                or not row['received_at'] <= policy._number(row['processed_at']) <= now):
            raise ValueError('known_group_raw_clock_unknown')
        update = pb.SubscribeUpdate()
        try:
            raw = decode_payload(row['payload_json'],archive_dir=self.capture_path.parent/'wallet-flow-archive')
            update.ParseFromString(raw)
        except DecodeError as exc:
            raise ValueError('known_group_raw_payload_invalid') from exc
        info = update.transaction.transaction
        # Avoid full account/token/native conversion for the bulk of the research stream.
        keys = {base58.b58encode(k).decode() for k in info.transaction.message.account_keys}
        keys.update(base58.b58encode(k).decode() for k in info.meta.loaded_writable_addresses)
        keys.update(base58.b58encode(k).decode() for k in info.meta.loaded_readonly_addresses)
        keys.update(b.owner for b in info.meta.pre_token_balances)
        keys.update(b.owner for b in info.meta.post_token_balances)
        if not keys.intersection(tracked):
            return None
        if row.get('block_time') is None:
            raise ValueError('known_group_block_time_pending')
        index = _explicit_index(info,raw)
        if index is None:
            raise ValueError('known_group_order_unknown')
        tx = confirmed_receipt(update,commitment='confirmed',block_time=row['block_time'])
        if tx['blockTime'] > row['received_at']:
            raise ValueError('known_group_chain_clock_future')
        tx['transactionIndex'] = index  # Raw optional presence proves zero; a protobuf default does not.
        if (tx['slot'] != row['slot'] or tx['transaction']['signatures'][0] != row['signature']
                or (row.get('tx_index') is not None and row['tx_index'] != index)):
            raise ValueError('known_group_raw_identity_mismatch')
        return tx,hashlib.sha256(raw).hexdigest(),_compiled_profile(update,tx)

    def _market(self, mint, receipt, now, beat):
        from sniper.services.memecoin_desk_ps_paper import _state
        from sniper.services.memecoin_desk import verdict_price_pumpswap,WSOL_MINT
        cut = min(now,policy._number(beat['ps_committed_through']))
        if cut < receipt:
            raise ValueError('known_group_no_tradable_pumpswap_before_deadline')
        observation,(base,quote,virtual),fees = _state(self.desk_dir,mint,cut,now,integrity=True)
        meta = db.known_group_mint_metadata(self.desk_dir,mint,now_host=cut)
        state_at = observation['state_at']
        slot = observation['proof']['slot']
        ps = dict(base_reserve_atomic=str(base),quote_reserve_atomic=str(quote),virtual_quote_atomic=str(virtual),
            supply_atomic=meta['supply_atomic'],quote_mint=WSOL_MINT,base_decimals=meta['decimals'],
            lp_fee_bps=fees[0],protocol_fee_bps=fees[1],creator_fee_bps=fees[2],slot=slot,
            metadata_known_at=meta['metadata_known_at'],metadata_slot=meta['metadata_slot'],metadata_source=meta['metadata_source'],
            reserve_known_at=state_at,virtual_quote_known_at=state_at,virtual_quote_slot=slot,
            virtual_quote_source='pumpswap_grpc:buy_event_virtual_quote',cut_slot=slot+1)
        sol_usd = policy._number(beat['sol_usd'])
        sol_age = beat['sol_age_s'] + now-beat['ts']
        cap = verdict_price_pumpswap(ps,as_of=now,sol_usd=sol_usd,sol_age_s=sol_age,clip_usd=policy.POLICY['clip_usd'])
        if cap is None:
            raise ValueError('known_group_verdict_price_unproven')
        return dict(venue='pumpswap',pool=observation['pool'],price_usd=cap['ref_price_usd'],observed_at_host=cut,
                    pool_state=ps,sol_usd=sol_usd,sol_age_s=sol_age,state_proof=observation['proof']),meta

    def _member_readiness(self, members, beat):
        from sniper.office.wallet_selector_identity import selector_digest
        path = self.desk_dir/'pumpswap/watch_wallets.json'
        stat = path.stat()
        fingerprint = (stat.st_dev,stat.st_ino,stat.st_mtime_ns,stat.st_size)
        if getattr(self,'_watch_fingerprint',None) != fingerprint:
            exported = json.loads(path.read_text())
            if exported.get('version') != 1 or not isinstance(exported.get('wallets'),list):
                raise ValueError('known_group_watch_export_unknown')
            wallets = frozenset(exported['wallets'])
            if any(not isinstance(w,str) or len(base58.b58decode(w))!=32 for w in wallets):
                raise ValueError('known_group_watch_export_unknown')
            self._watch_fingerprint,self._exported_wallets = fingerprint,wallets
            self._exported_digest = selector_digest(wallets)
        if (len(self._exported_wallets) != beat['wallet_flow']['submitted_selectors']
                or self._exported_digest != beat['wallet_flow'].get('submitted_selector_digest')):
            raise ValueError('known_group_watch_export_not_submitted')
        for member in members:
            if (member['address'] not in self._exported_wallets
                    or not db.known_group_watch_active(self.watch_path,member['address'])):
                raise ValueError('known_group_member_observation_not_ready')

    def _proof(self, candidate, state, now, now_iso, beat):
        fact = candidate['fact']
        receipt = candidate['creation']['received_at_host']
        saved_path = state['funding'].get(fact['developer'])
        path = ([dict(h,transaction=self._native_profile(h['transaction_ref'])) for h in saved_path]
                if saved_path else None)
        if not path or any(h['known_at_host'] is None or h['known_at_host'] >= receipt for h in path):
            raise ValueError('known_group_prior_funding_unknown')
        tradable,metadata = self._market(fact['mint'],receipt,now,beat)
        if not db.known_group_watch_active(self.watch_path,fact['developer']):
            raise ValueError('known_group_member_observation_not_ready')
        creation = dict(candidate['creation'],decimals=metadata['decimals'],
                        transaction=self._native_profile(candidate['creation']['transaction_ref']))
        members = [dict(address=fact['developer'],role='developer',proof_ref=creation['raw_ref'],known_at_host=candidate['known_at_host'])]
        funder = path[-1]['funder']
        for address,funding in state['funding'].items():
            hop = funding[-1]
            if (address == fact['developer'] or hop.get('known_at_host') is None
                    or hop['known_at_host'] >= receipt or hop['funder'] != funder):
                continue
            if (int(hop['amount_atomic']) >= policy.POLICY['funding_min_lamports']
                    and 0 <= fact['block_time']-hop['block_time'] <= policy.POLICY['funding_window_s']):
                hop = dict(hop,transaction=self._native_profile(hop['transaction_ref']))
                members.append(dict(address=address,role='funded',proof_ref=hop['proof_ref'],
                                    known_at_host=hop['known_at_host'],funding=hop))
        seen = {m['address'] for m in members}
        for member in candidate.get('genesis',[]):
            if member['address'] not in seen:
                members.append(dict(member,transaction=self._native_profile(member['transaction_ref'])))
                seen.add(member['address'])
        coverage = dict(state='observed',connected=True,gap=False,overflow=False,
                        history_basis='observed_since_tail',complete=False,
                        selector_acknowledged=beat['wallet_flow'].get('selector_acknowledged'),
                        submitted_selector_digest=beat['wallet_flow'].get('submitted_selector_digest'),
                        selector_changes=copy.deepcopy(state['selector_changes']),
                        baseline_at_host=state['baseline_at_host'],source_heartbeat_at_host=beat['ts'])
        prior = [sell for sell in state.get('sells',{}).get(fact['mint'],[]) if sell['owner'] in seen]
        return dict(version='known-group-launch-proof-v1',cohort_id=policy.COHORT_ID,
            registry=dict(version='GRP-001-v1',sha256=policy.REGISTRY_HASH,root=path[0]['funder'],
                          approved_at_host=state['baseline_at_host'],source='host'),
            creation=creation,funding_path=path,members=members,tradable=tradable,coverage=coverage,prior_sells=prior)

    def _manifest(self, proof):
        manifest = {key:copy.deepcopy(value) for key,value in proof.items() if key not in ('creation','funding_path','members')}
        manifest['creation'] = {key:value for key,value in proof['creation'].items() if key!='transaction'}
        manifest['funding_path'] = [{key:value for key,value in hop.items() if key!='transaction'} for hop in proof['funding_path']]
        manifest['members'] = []
        for member in proof['members']:
            saved = {key:value for key,value in member.items() if key not in ('transaction','funding')}
            if 'funding' in member:
                saved['funding'] = {key:value for key,value in member['funding'].items() if key!='transaction'}
            manifest['members'].append(saved)
        return manifest

    def _hydrate(self, manifest):
        proof = copy.deepcopy(manifest)
        proof['creation']['transaction'] = self._native_profile(proof['creation']['transaction_ref'])
        for hop in proof['funding_path']:
            hop['transaction'] = self._native_profile(hop['transaction_ref'])
        for member in proof['members']:
            if 'transaction_ref' in member:
                member['transaction'] = self._native_profile(member['transaction_ref'])
            if 'funding' in member:
                member['funding']['transaction'] = self._native_profile(member['funding']['transaction_ref'])
        return proof

    def tick(self, limit=KNOWN_GROUP_TAIL_PAGE):
        if type(limit) is not int or not 1 <= limit <= KNOWN_GROUP_TAIL_PENDING_MAX:
            raise ValueError('known_group_tail_page_bound')
        try:
            now,now_iso = policy._number(self.clock()),self.now_iso_fn()
            anchor_host,anchor_utc = now,policy._stamp(now_iso)
            tail = db.known_group_tail_state(self.observer,str(self.capture_path))
            initial = tail is None
            state = (dict(funding={},candidates={},pending={},sells={},baseline_at_host=now,gap_at_host=None,status='starting')
                     if initial else json.loads(tail['state_json']))
        except Exception:
            logger.exception('known-group observer clock/state unavailable')
            db.invalidate_known_group_observer(self.conn,now_iso=None,reason='known_group_clock_or_state_unknown')
            return dict(state='unknown',reason='known_group_clock_or_state_unknown',rows=0,eligible=0)
        try:
            if tail and now < tail['last_host']:
                raise ValueError('known_group_clock_regressed')
            page = db.read_known_group_raw_tail(self.capture_path,cursor=tail['cursor'] if tail else 0,
                pending=state['pending'],anchor=tail['anchor'] if tail else None,limit=limit,now_host=now)
            row_observed = policy._number(self.clock())
            if row_observed < now:
                raise ValueError('known_group_clock_regressed')
            if tail and tail['identity'] != page['identity']:
                raise ValueError('known_group_source_replaced')
            if initial:
                maximum = page['maximum']
                cursor = maximum['flow_id'] if maximum else 0
                anchor = json.dumps([maximum[k] for k in ('signature','slot','received_at')]) if maximum else None
                tail = dict(identity=page['identity'],cursor=cursor,anchor=anchor,baseline=cursor,started_at=now)
                state.update(status='baseline_unknown_history')
                self._save(tail,state,now)
                return dict(state='baseline',reason='unknown_history',cursor=cursor,rows=0)
            health_reason = None
            try:
                beat = self._health(now)
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError,OSError) as exc:
                health_reason = str(exc) if isinstance(exc,ValueError) else 'known_group_source_health_unknown'
                if state.get('health_reason') != health_reason:
                    logger.exception('known-group source health unavailable; entries refused, positive sells still observed')
                db.invalidate_known_group_observer(self.conn,now_iso=now_iso,reason=health_reason)
                beat = None
            state['health_reason'] = health_reason
            service_reason = None
            try:
                services = policy._known_services()
            except (OSError,ValueError):
                logger.exception('known-group services unknown; entries refused, positive sells still observed')
                services,service_reason = None,'known_group_service_catalogue_unknown'
                db.invalidate_known_group_observer(self.conn,now_iso=now_iso,reason=service_reason)
            rejected_paths = 0
            if services is not None:
                safe = {owner:path for owner,path in state['funding'].items()
                        if policy._service_path_allowed(path,services)}
                rejected_paths = len(state['funding'])-len(safe)
                state['funding'] = safe
            # A local selector edit reports no lost frame. Keep its exact producer diagnostics;
            # current digest/member readiness below still decides whether observation is usable.
            state['selector_changes'] = dict(
                pending=copy.deepcopy(beat['wallet_flow']['pending_gaps'].get('selectors_changed')) if beat else None,
                persisted=[gap for gap in page['gaps'] if gap['reason'] == 'selectors_changed'])
            positions = db.known_group_open_positions(self.conn)
            active_mints = db.known_group_candidate_mints(self.conn,now_iso=now_iso)|{p['mint'] for p in positions}
            tracked = set(policy.APPROVED_ROOTS)|set(state['funding'])
            for position in positions:
                frozen = json.loads(position['funded_contract_json'])['source']['desk']['known_group_proof']
                tracked.update(m['address'] for m in frozen['members'])
            from sniper.services.wallet_identity import verified_sol_funding
            from sniper.office.wallet_launch_proof import launch_facts
            work = {r['flow_id']:r for r in page['retry']+page['rows']}
            status = dict(state='unknown' if health_reason or service_reason else 'observed',rows=0,eligible=0,
                          refusals={health_reason:1} if health_reason else {},capture_commit_delay_s=[],
                          selector_changes=state['selector_changes'])
            if health_reason:
                status['reason'] = health_reason
            if service_reason:
                status['reason'] = service_reason
                status['refusals'][service_reason] = 1
            if rejected_paths:
                status['refusals']['known_group_service_hop'] = rejected_paths
            for flow_id,row in sorted(work.items()):
                current = policy._number(self.clock())
                if current < now:
                    raise ValueError('known_group_clock_regressed')
                now = current
                status['rows'] += 1
                if flow_id > tail['cursor']:
                    tail['cursor'] = flow_id
                    tail['anchor'] = json.dumps([row[k] for k in ('signature','slot','received_at')])
                receipt_utc = state['pending'].get(str(flow_id),{}).get('receipt_utc')
                if receipt_utc is None:
                    receipt_utc = (anchor_utc+timedelta(seconds=row['received_at']-anchor_host)).isoformat()
                try:
                    decoded = self._decode(row,tracked,now)
                except ValueError as exc:
                    reason = str(exc)
                    status['refusals'][reason] = status['refusals'].get(reason,0)+1
                    if reason == 'known_group_block_time_pending' and now-row['received_at'] < policy.POLICY['entry_ttl_s']:
                        state['pending'][str(flow_id)] = dict(receipt_utc=receipt_utc,received_at=row['received_at'])
                    else:
                        state['pending'].pop(str(flow_id),None)
                        state['gap_at_host'] = now
                    continue
                state['pending'].pop(str(flow_id),None)
                if decoded is None:
                    continue
                tx,raw_hash,compiled = decoded
                decoded_at = policy._number(self.clock())
                if decoded_at < now:
                    raise ValueError('known_group_clock_regressed')
                now = decoded_at
                ref = f'{self.capture_path}:{flow_id}:{row["signature"]}'
                envelope = dict(transaction=tx,commitment='confirmed',source='raw_confirmed_tail',flow_id=flow_id,
                    raw_ref=ref,received_at_host=row['received_at'],decoder_ready_at_host=now,
                    committed_at_host=now,slot=row['slot'],tx_index=tx['transactionIndex'],raw_sha256=raw_hash,
                    committed_row_observed_at_host=row_observed,capture_processed_at_host=row['processed_at'])
                status['capture_commit_delay_s'].append(now-row['received_at'])
                cache_needed = False
                for clue in verified_sol_funding(tx,now_iso):
                    sender,recipient = clue['funder'],clue['recipient']
                    if services is None or sender in services['addresses'] or recipient in services['addresses']:
                        continue
                    if sender not in policy.APPROVED_ROOTS and sender not in state['funding']:
                        continue
                    path = [] if sender in policy.APPROVED_ROOTS else state['funding'][sender]
                    if len(path) >= 8:
                        continue
                    hop = dict(source='host',proof_state='verified',proof_kind='research_stream',
                        method='wallet-watch-stream-v1',proof_ref=ref,known_at_host=None,transaction_ref=ref,
                        commitment='confirmed',slot=tx['slot'],tx_index=tx['transactionIndex'],
                        funder=sender,recipient=recipient,amount_atomic=clue['amount_atomic'],raw_sha256=raw_hash,
                        block_time=tx['blockTime'])
                    state['funding'][recipient] = path+[hop]
                    tracked.add(recipient)
                    cache_needed = True
                for fact in launch_facts(tx,commitment='confirmed')['items']:
                    if fact['developer'] not in state['funding']:
                        continue
                    source = {k:v for k,v in envelope.items() if k!='transaction'}
                    state['candidates'].setdefault(fact['signature'],dict(fact=fact,
                        creation=dict(source,received_at_iso=receipt_utc,transaction_ref=ref),known_at_host=now))
                    cache_needed = True
                before,after = tx['meta']['preTokenBalances'],tx['meta']['postTokenBalances']
                if before and after:
                    signers = {k['pubkey'] for k in tx['transaction']['message']['accountKeys'] if k['signer']}
                    for candidate in state['candidates'].values():
                        fact = candidate['fact']
                        if (tx['slot'] != fact['slot'] or tx['transactionIndex'] < candidate['creation']['tx_index']):
                            continue
                        for owner in signers:
                            pre = sum(int(b['uiTokenAmount']['amount']) for b in before if b['owner']==owner and b['mint']==fact['mint'])
                            post = sum(int(b['uiTokenAmount']['amount']) for b in after if b['owner']==owner and b['mint']==fact['mint'])
                            if post > pre and owner != fact['developer']:
                                candidate.setdefault('genesis',[]).append(dict(address=owner,role='genesis',
                                    proof_ref=row['signature'],known_at_host=now,transaction_ref=ref,commitment='confirmed',raw_sha256=raw_hash))
                                cache_needed = True
                from sniper.services.wallet_universe_sol_venues import decode_transaction
                mints = {b['mint'] for b in tx['meta']['preTokenBalanceObservations']+tx['meta']['postTokenBalanceObservations']}
                for mint in mints:
                    result = decode_transaction(compiled,mint)
                    for swap in result.get('swaps',[]):
                        if (swap.get('actor_verified') is not True or swap['protocol'] != 'pumpswap'
                                or int(swap['mint_delta_atomic']) <= 0 or int(swap['quote']['amount']) >= 0):
                            continue
                        sell = dict(envelope,transaction=compiled,mint=mint,owner=swap['actor'],signature=row['signature'])
                        descriptor = {k:v for k,v in sell.items() if k!='transaction'}
                        descriptor['transaction_ref'] = ref
                        saved = state.setdefault('sells',{}).setdefault(mint,[])
                        if not any(s['signature']==row['signature'] and s['owner']==swap['actor'] for s in saved):
                            saved.append(descriptor)
                        cache_needed = True
                        if mint in active_mints:
                            policy.ingest_sell(self.conn,sell,now_host=now,now_iso=self.now_iso_fn())
                if cache_needed:
                    db.store_known_group_evidence(self.observer,ref,
                        dict(version='known-group-native-profile-v1',parsed=tx,compiled=compiled))
                self._save(tail,state,now)
                # This post-commit witness is availability, not the original source receipt clock.
                known = policy._number(self.clock())
                for path in state['funding'].values():
                    if path[-1]['known_at_host'] is None:
                        path[-1]['known_at_host'] = known
            # A signed entry can land after the sell was decoded; retain the original freshness clock.
            for position in db.known_group_open_positions(self.conn):
                for saved in state.get('sells',{}).get(position['mint'],[]):
                    decision = policy._number(self.clock())
                    age = decision-saved['received_at_host']
                    if not 0 <= age <= policy.POLICY['sell_freshness_s']:
                        status['refusals']['known_group_sell_stale'] = status['refusals'].get('known_group_sell_stale',0)+1
                        continue
                    sell = {k:v for k,v in saved.items() if k!='transaction_ref'}
                    sell['transaction'] = self._native_profile(saved['transaction_ref'],compiled=True)
                    policy.nominate_sell(self.conn,position['id'],sell,now_host=decision,now_iso=self.now_iso_fn())
            if len(state['pending']) > KNOWN_GROUP_TAIL_PENDING_MAX:
                raise ValueError('known_group_pending_overflow')
            for signature,candidate in list(state['candidates'].items()):
                decision = policy._number(self.clock())
                if decision < now:
                    raise ValueError('known_group_clock_regressed')
                now = decision
                receipt = candidate['creation']['received_at_host']
                if now >= receipt+policy.POLICY['entry_ttl_s']:
                    status['refusals']['known_group_original_deadline_expired'] = status['refusals'].get('known_group_original_deadline_expired',0)+1
                    del state['candidates'][signature]
                    continue
                if ((state.get('gap_at_host') or 0) >= receipt
                        or any(g['reason'] != 'selectors_changed' and g['last_at'] >= receipt
                               and g['known_at'] <= now for g in page['gaps'])):
                    status['refusals']['known_group_observed_gap'] = status['refusals'].get('known_group_observed_gap',0)+1
                    continue
                try:
                    if service_reason:
                        raise ValueError(service_reason)
                    if beat is None:
                        raise ValueError(health_reason)
                    proof = (self._hydrate(candidate['prepared']) if candidate.get('prepared')
                             else self._proof(candidate,state,now,self.now_iso_fn(),beat))
                    # Frozen evidence is immutable; subscription readiness is current on every attempt.
                    self._member_readiness(proof['members'],beat)
                    owners = {member['address'] for member in proof['members']}
                    if any(sell['owner'] in owners for sell in state.get('sells',{}).get(candidate['fact']['mint'],[])):
                        candidate['invalidated'] = True
                    if candidate.get('invalidated'):
                        raise ValueError('known_group_prior_sell')
                    authority = db._known_group_policy_refusal(self.conn)
                    if authority:
                        raise ValueError(authority)
                    candidate['prepared'] = self._manifest(proof)
                    self._save(tail,state,now)
                    policy.record_candidate(self.conn,proof,now_host=now,now_iso=self.now_iso_fn())
                    policy.enqueue_candidate(self.conn,proof,now_host=now,now_iso=self.now_iso_fn())
                    status['eligible'] += 1
                    del state['candidates'][signature]
                except (ValueError,OSError) as exc:
                    reason = str(exc) if isinstance(exc,ValueError) else 'known_group_local_proof_unreadable'
                    status['refusals'][reason] = status['refusals'].get(reason,0)+1
            # Root-to-relay ancestry stays prior-known across idle periods/restarts. Only a
            # developer's final payment (and sibling membership) has the 60-second window.
            state['sells'] = {mint:[s for s in sells if now-s['received_at_host']<=policy.POLICY['funding_window_s']]
                             for mint,sells in state.get('sells',{}).items()}
            state['sells'] = {mint:sells for mint,sells in state['sells'].items() if sells}
            state['status'] = status
            self._save(tail,state,now)
            return status
        except Exception as exc:
            logger.exception('known-group raw tail unavailable')
            reason = str(exc) if isinstance(exc,ValueError) else 'known_group_observer_unreadable'
            db.invalidate_known_group_observer(self.conn,now_iso=now_iso,reason=reason)
            if tail and 'last_host' in tail and now >= tail['last_host']:
                state.update(gap_at_host=now,status=dict(state='unknown',reason=reason))
                self._save(tail,state,now)
            return dict(state='unknown',reason=reason,rows=0)

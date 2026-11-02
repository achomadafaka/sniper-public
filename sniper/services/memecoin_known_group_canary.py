"""Prior-known launch experiment. Pure proof checks; funding stays in the existing desk lane."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import logging
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from sniper.settings.known_group import KNOWN_GROUP_LAUNCH_V1,KNOWN_GROUP_PROOF_MAX_BYTES,KNOWN_GROUP_SERVICE_CATALOGUE

logger = logging.getLogger(__name__)

COHORT_ID = 'known-group-launch-v1'
PROMPT_VERSION = 'known-group-launch-v1'
REGISTRY_HASH = 'eb81b2da4c235e8b8396ef5abb985d0c647d40776ba21c52d175dc546f899ba4'
APPROVED_ROOTS = frozenset({
    'AJZoKpE4xJvDFUj5fjCj2dUajJai9TvbgL5h2SgTiUax',
    '9q5yxUcz9HbfEFG9Ww88h9GGUSWsJV755cUF4AhqSmU1',
    'ADvxxVG6A3BSnnCCawtZL9dqGzgFCR3hd9F1XRoXHWP9',
})
POLICY = dict(KNOWN_GROUP_LAUNCH_V1)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _known_services():
    """Local data only, off the signing path; changed or unreadable sources remain unknown."""
    try:
        from sniper.services.memecoin_cluster_rpc import known_funders,is_address
        from sniper.services.memecoin_fee_reconciler import tip_accounts
        from sniper.services.memecoin_desk_txshape import JITO_TIP_ACCOUNTS,CORE_PROGRAMS
        known = known_funders()
        addresses = frozenset(known['addresses']) | tip_accounts() | JITO_TIP_ACCOUNTS | CORE_PROGRAMS
        body = dict(version=KNOWN_GROUP_SERVICE_CATALOGUE['version'],
            known_funders_version=known['version'],known_funders_sha256=known['sha256'],addresses=sorted(addresses))
        if (not addresses or any(not is_address(address) for address in addresses)
                or known['version'] != KNOWN_GROUP_SERVICE_CATALOGUE['known_funders_version']
                or known['sha256'] != KNOWN_GROUP_SERVICE_CATALOGUE['known_funders_sha256']
                or hashlib.sha256(_json(body).encode()).hexdigest() != KNOWN_GROUP_SERVICE_CATALOGUE['sha256']):
            raise ValueError('local service catalogue identity changed')
        return MappingProxyType(dict(KNOWN_GROUP_SERVICE_CATALOGUE,addresses=addresses))
    except (ImportError,OSError,ValueError,KeyError,TypeError) as exc:
        logger.exception('known-group local service catalogue unavailable')
        raise ValueError('known_group_service_catalogue_unknown') from exc


def _service_path_allowed(path, services):
    return bool(path) and all(type(hop) is dict
        and all(isinstance(hop.get(key),str) and hop[key] and hop[key] not in services['addresses']
                for key in ('funder','recipient')) for hop in path)


_POLICY_JSON = _json(POLICY)
_POLICY_BODY = _json(dict(producer='H', prompt_version=PROMPT_VERSION,
    policy=POLICY, kill_limit_usd=0.0, stop_mode='manual'))
POLICY_HASH = hashlib.sha256(_POLICY_BODY.encode()).hexdigest()


def _valid_policy(policy, producer, prompt_version):
    try:
        return producer == 'H' and prompt_version == PROMPT_VERSION and _json(policy) == _POLICY_JSON
    except (TypeError,ValueError):
        return False


def _stamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.utcoffset() != timedelta(0):
        raise ValueError('known_group_utc_unknown')
    return stamp


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('known_group_clock_unknown')
    return float(value)


def _identity(envelope, tx):
    if (type(envelope.get('slot')) is not int or envelope['slot'] <= 0
            or tx.get('slot') != envelope['slot'] or type(envelope.get('tx_index')) is not int
            or envelope['tx_index'] < 0 or tx.get('transactionIndex') != envelope['tx_index']):
        raise ValueError('known_group_order_unknown')


def _candidate_identity(proof):
    if proof.get('version') == 'known-group-launch-compact-v1':
        creation = proof['creation']
        if (proof.get('cohort_id') != COHORT_ID or creation.get('source') != 'raw_confirmed_tail'
                or any(not isinstance(creation.get(key),str) or not creation[key] for key in
                       ('mint','developer','curve','signature','raw_ref','raw_sha256'))):
            raise ValueError('known_group_compact_identity_invalid')
        return {key:creation[key] for key in ('mint','developer','curve','signature','slot','block_time','outer_ix')}
    tx = proof['creation']['transaction']
    from sniper.office.wallet_launch_proof import launch_facts
    facts = launch_facts(tx, commitment=proof['creation']['commitment'])
    if facts['state'] != 'known' or len(facts['items']) != 1:
        raise ValueError('known_group_creation_unproven')
    return facts['items'][0]


def _raw_digest(envelope, transaction):
    digest = envelope.get('raw_sha256')
    if digest is not None:
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('known_group_raw_hash_invalid')
        return digest,'protobuf_sha256'
    return hashlib.sha256(_json(transaction).encode()).hexdigest(),'rpc_json_sha256'


def _compact(proof, fact, *, now_host, deadline):
    from sniper.services.wallet_identity import verified_sol_funding
    cr = proof['creation']
    creation = {key:cr[key] for key in ('source','flow_id','raw_ref','received_at_host','received_at_iso',
        'decoder_ready_at_host','committed_at_host','slot','tx_index','decimals')}
    creation.update({key:cr[key] for key in ('committed_row_observed_at_host','capture_processed_at_host') if key in cr})
    creation.update({key:fact[key] for key in ('mint','developer','curve','signature','block_time','outer_ix')})
    creation['raw_sha256'],creation['raw_hash_kind'] = _raw_digest(cr,cr['transaction'])
    path = []
    owner = proof['registry']['root']
    for index,hop in enumerate(proof['funding_path']):
        destination = fact['developer'] if index==len(proof['funding_path'])-1 else hop['recipient']
        clue = next(c for c in verified_sol_funding(hop['transaction'],cr['received_at_iso'])
                    if c['funder']==owner and c['recipient']==destination)
        payment = dict(funder=owner,recipient=destination,amount_atomic=clue['amount_atomic'],
            signature=clue['funding_signature'],slot=hop['slot'],tx_index=hop['tx_index'],block_time=clue['funded_at'],
            known_at_host=hop['known_at_host'],proof_ref=hop['proof_ref'],method=hop['method'],payment_method=clue['method'])
        payment['raw_sha256'],payment['raw_hash_kind'] = _raw_digest(hop,hop['transaction'])
        path.append(payment)
        owner = destination
    members = []
    for member in proof['members']:
        compact = {key:member[key] for key in ('address','role','proof_ref','known_at_host')}
        if member['role']=='developer':
            digest,kind = creation['raw_sha256'],creation['raw_hash_kind']
        elif member['role']=='genesis':
            digest,kind = _raw_digest(member,member['transaction'])
        else:
            payment = next((h for h in path if h['proof_ref']==member['proof_ref']),None)
            if payment:
                digest,kind = payment['raw_sha256'],payment['raw_hash_kind']
            else:
                hop = member['funding']
                digest,kind = _raw_digest(hop,hop['transaction'])
        compact.update(raw_sha256=digest,raw_hash_kind=kind)
        members.append(compact)
    return dict(version='known-group-launch-compact-v1',cohort_id=COHORT_ID,
        registry=copy.deepcopy(proof['registry']),service_catalogue=dict(KNOWN_GROUP_SERVICE_CATALOGUE),
        creation=creation,funding_path=path,members=members,
        tradable=copy.deepcopy(proof['tradable']),coverage=copy.deepcopy(proof['coverage']),
        full_source_sha256=hashlib.sha256(_json(proof).encode()).hexdigest(),
        decision_cut_host=now_host,entry_deadline_utc=deadline.isoformat())


def _validated(proof, now_host, now_iso):
    from sniper.services.wallet_identity import verified_sol_funding
    now_host = _number(now_host)
    now = _stamp(now_iso)
    if (type(proof) is not dict or proof.get('version') != 'known-group-launch-proof-v1'
            or proof.get('cohort_id') != COHORT_ID):
        raise ValueError('known_group_proof_invalid')
    try:
        services = _known_services()
    except (OSError,ValueError) as exc:
        raise ValueError('known_group_service_catalogue_unknown') from exc
    registry, creation, coverage = proof['registry'], proof['creation'], proof['coverage']
    receipt = _number(creation['received_at_host'])
    if not receipt <= now_host < receipt + POLICY['entry_ttl_s']:
        raise ValueError('known_group_entry_expired')
    if (registry.get('source') != 'host' or registry.get('version') != 'GRP-001-v1'
            or registry.get('sha256') != REGISTRY_HASH or registry.get('root') not in APPROVED_ROOTS
            or not _number(registry['approved_at_host']) < receipt):
        raise ValueError('known_group_registry_unproven')
    if (creation.get('source') != 'raw_confirmed_tail' or creation.get('commitment') != 'confirmed'
            or type(creation.get('flow_id')) is not int or creation['flow_id'] <= 0
            or not isinstance(creation.get('raw_ref'), str) or not creation['raw_ref']
            or not receipt <= _number(creation['decoder_ready_at_host']) <= now_host
            or not receipt <= _number(creation['committed_at_host']) <= now_host):
        raise ValueError('known_group_receipt_unproven')
    receipt_utc = _stamp(creation['received_at_iso'])
    # Validate the supplied clock conversion once; the stored receipt UTC owns all later deadlines.
    if abs((now - receipt_utc).total_seconds() - (now_host - receipt)) > POLICY['sell_freshness_s']:
        raise ValueError('known_group_clock_regressed')
    if now >= receipt_utc + timedelta(seconds=POLICY['entry_ttl_s']):
        raise ValueError('known_group_entry_expired')
    tx = creation['transaction']
    _identity(creation, tx)
    if type(creation.get('decimals')) is not int or not 0 <= creation['decimals'] <= 18:
        raise ValueError('known_group_decimals_unknown')
    fact = _candidate_identity(proof)
    if not 0 < fact['block_time'] <= receipt:
        raise ValueError('known_group_creation_clock_unknown')
    path = proof['funding_path']
    if not isinstance(path, list) or not 1 <= len(path) <= 8:
        raise ValueError('known_group_funding_unproven')
    owner = registry['root']
    proven_funding = set()
    prior_order,prior_time = None,None
    for index,hop in enumerate(path):
        if (hop.get('source') != 'host' or hop.get('proof_state') != 'verified'
                or hop.get('proof_kind') != 'research_stream' or hop.get('method') != 'wallet-watch-stream-v1'
                or not isinstance(hop.get('proof_ref'), str) or not hop['proof_ref']
                or hop.get('commitment') not in ('confirmed', 'finalized')
                or not _number(hop['known_at_host']) < receipt):
            raise ValueError('known_group_funding_knowledge_unknown')
        payment = hop['transaction']
        _identity(hop, payment)
        if (payment['blockTime'] > fact['block_time']
                or (payment['slot'], hop['tx_index']) >= (fact['slot'], creation['tx_index'])):
            raise ValueError('known_group_funding_not_prior')
        if prior_order is not None and ((payment['slot'],hop['tx_index'])<=prior_order or payment['blockTime']<prior_time):
            raise ValueError('known_group_funding_path_order_unknown')
        prior_order,prior_time = (payment['slot'],hop['tx_index']),payment['blockTime']
        clues = verified_sol_funding(payment, now_iso)
        destination = (fact['developer'] if index == len(path)-1 else hop.get('recipient'))
        if owner in services['addresses'] or destination in services['addresses']:
            raise ValueError('known_group_service_hop')
        matches = [c for c in clues if c['funder'] == owner and c['recipient'] == destination]
        if len(matches) != 1:
            raise ValueError('known_group_funding_path_unbound')
        clue = matches[0]
        owner = clue['recipient']
        proven_funding.add((owner, hop['proof_ref']))
    if (owner != fact['developer'] or int(clue['amount_atomic']) < POLICY['funding_min_lamports']
            or not 0 <= fact['block_time'] - clue['funded_at'] <= POLICY['funding_window_s']):
        raise ValueError('known_group_developer_funding_unproven')
    members = proof['members']
    if not isinstance(members, list) or not members or len(members) > 512:
        raise ValueError('known_group_members_unknown')
    seen = set()
    for member in members:
        address, role, ref = member['address'], member['role'], member['proof_ref']
        if address in services['addresses']:
            raise ValueError('known_group_service_hop')
        if address in seen or not _number(member['known_at_host']) <= now_host:
            raise ValueError('known_group_member_cut_invalid')
        seen.add(address)
        if role == 'developer':
            if address != fact['developer'] or ref != creation['raw_ref']:
                raise ValueError('known_group_developer_unbound')
        elif role == 'funded':
            if (address, ref) not in proven_funding:
                payment = member['funding']
                _identity(payment, payment['transaction'])
                if (payment.get('source') != 'host' or payment.get('commitment') != 'confirmed'
                        or payment.get('proof_ref') != ref or not _number(payment['known_at_host']) < receipt
                        or not 0 <= fact['block_time']-payment['transaction']['blockTime'] <= POLICY['funding_window_s']
                        or (payment['slot'],payment['tx_index']) >= (fact['slot'],creation['tx_index'])
                        or not any(c['funder']==clue['funder'] and c['recipient']==address
                            and int(c['amount_atomic'])>=POLICY['funding_min_lamports']
                            for c in verified_sol_funding(payment['transaction'],now_iso))):
                    raise ValueError('known_group_member_funding_unbound')
        elif role == 'genesis':
            genesis = member['transaction']
            if (member.get('commitment') != 'confirmed' or genesis['slot'] != fact['slot']
                    or genesis['blockTime'] != fact['block_time']
                    or type(genesis.get('transactionIndex')) is not int or genesis['transactionIndex'] < creation['tx_index']
                    or not _number(member['known_at_host']) >= receipt):
                raise ValueError('known_group_genesis_unproven')
            keys = genesis['transaction']['message']['accountKeys']
            signers = {k['pubkey'] for k in keys if k.get('signer') is True}
            before,after = genesis['meta']['preTokenBalances'],genesis['meta']['postTokenBalances']
            if (not before or not after or {(b['accountIndex'],b['mint'],b['owner']) for b in before}
                    != {(b['accountIndex'],b['mint'],b['owner']) for b in after}):
                raise ValueError('known_group_genesis_inventory_unknown')
            pre = sum(int(b['uiTokenAmount']['amount']) for b in before
                      if b['owner'] == address and b['mint'] == fact['mint'])
            post = sum(int(b['uiTokenAmount']['amount']) for b in after
                       if b['owner'] == address and b['mint'] == fact['mint'])
            if (address not in signers or post <= pre or genesis['meta']['err'] is not None
                    or ref != genesis['transaction']['signatures'][0]):
                raise ValueError('known_group_genesis_unproven')
        else:
            raise ValueError('known_group_member_role_unknown')
    if fact['developer'] not in seen:
        raise ValueError('known_group_developer_member_missing')
    if (coverage.get('state') != 'observed' or coverage.get('connected') is not True
            or coverage.get('gap') is not False or coverage.get('overflow') is not False
            or coverage.get('history_basis') != 'observed_since_tail' or coverage.get('complete') is not False):
        raise ValueError('known_group_observer_unknown')
    if proof['prior_sells']:
        raise ValueError('known_group_prior_sell')
    tradable = proof['tradable']
    if (tradable.get('venue') != POLICY['venue'] or not tradable.get('pool')
            or not receipt <= _number(tradable['observed_at_host']) <= now_host
            or not _number(tradable['price_usd'])):
        raise ValueError('known_group_tradable_unknown')
    return fact, receipt_utc + timedelta(seconds=POLICY['entry_ttl_s'])


def evaluate_candidate(proof, *, now_host, now_iso):
    """Validate saved evidence; this pure result supplies no durable funding authority."""
    try:
        fact, deadline = _validated(proof, now_host, now_iso)
        from sniper.services.memecoin_desk import verdict_price_pumpswap
        tradable = proof['tradable']
        cap = verdict_price_pumpswap(tradable.get('pool_state'), as_of=now_host,
            sol_usd=tradable.get('sol_usd'), sol_age_s=tradable.get('sol_age_s'), clip_usd=POLICY['clip_usd'])
        if (cap is None or tradable['pool_state']['base_decimals'] != proof['creation']['decimals']
                or abs(cap['ref_price_usd'] - tradable['price_usd']) > cap['ref_price_usd'] * 1e-9):
            raise ValueError('known_group_verdict_price_unproven')
        compact = _compact(proof,fact,now_host=now_host,deadline=deadline)
        digest = hashlib.sha256(_json(compact).encode()).hexdigest()
        ticket = 'known-group:' + hashlib.sha256(_json([COHORT_ID, fact['mint'], fact['signature']]).encode()).hexdigest()
        request = dict(mint=fact['mint'], network_id='solana', signal_source='desk', size_usd=POLICY['clip_usd'],
            rails={'tp_pct':POLICY['tp_pct'], 'sl_pct':POLICY['sl_pct']}, rails_mode='custom', desk_ticket_id=ticket,
            origin_event_id='desk:' + ticket, ttl_s=(deadline - _stamp(now_iso)).total_seconds(),
            desk_metadata=dict(cohort_id=COHORT_ID, producer='H', opportunity_id=ticket,
                snapshot_hash=digest, time_stop_s=POLICY['time_stop_s'], known_group_proof=compact,
                entry_deadline_utc=deadline.isoformat(), verdict_price=cap,
                known_group_policy_hash=POLICY_HASH,known_group_cut_host=now_host))
        if len(_json(request['desk_metadata']).encode()) > KNOWN_GROUP_PROOF_MAX_BYTES:
            raise ValueError('known_group_compact_proof_oversized')
        return dict(eligible=True, reason=None, request=request)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('known_group_') else 'known_group_proof_invalid'
        return dict(eligible=False, reason=reason, request=None)


def register(conn, *, now_iso):
    from sniper.persistence import db_memecoin as db
    return db.register_desk_cohort(conn, producer='H', policy=POLICY, prompt_version=PROMPT_VERSION,
        created_at=now_iso, cohort_id=COHORT_ID, stop_mode='manual')


def set_admission(conn, *, enabled, confirm, policy_hash, now_iso):
    from sniper.persistence import db_memecoin as db
    if confirm is not True or type(enabled) is not bool:
        raise ValueError('known_group_operator_confirmation_required')
    _stamp(now_iso)
    return db.set_known_group_admission(conn, enabled=enabled, policy_hash=policy_hash, now_iso=now_iso)


def record_candidate(conn, proof, *, now_host, now_iso):
    from sniper.persistence import db_memecoin as db
    result = evaluate_candidate(proof, now_host=now_host, now_iso=now_iso)
    if not result['eligible']:
        raise ValueError(result['reason'])
    request = result['request']
    return db.record_known_group_candidate(conn, request=request,
        proof_json=_json(request['desk_metadata']['known_group_proof']),now_iso=now_iso)


def _admission_refusal(conn, meta, *, mint, now_iso, now_host=None):
    if not isinstance(meta, dict) or meta.get('cohort_id') != COHORT_ID:
        return None
    try:
        if meta['known_group_proof'].get('service_catalogue') != dict(KNOWN_GROUP_SERVICE_CATALOGUE):
            return 'known_group_service_catalogue_unproven'
        if (mint != _candidate_identity(meta['known_group_proof'])['mint']
                or meta['time_stop_s'] != POLICY['time_stop_s'] or meta['producer'] != 'H'
                or meta['snapshot_hash'] != hashlib.sha256(_json(meta['known_group_proof']).encode()).hexdigest()
                or _stamp(now_iso) >= _stamp(meta['entry_deadline_utc'])):
            return 'known_group_entry_invalid_or_expired'
        from sniper.persistence import db_memecoin as db
        return db.known_group_candidate_refusal(conn, meta, now_iso=now_iso)
    except (ValueError, TypeError, KeyError, AttributeError):
        return 'known_group_metadata_invalid'


def enqueue_candidate(conn, proof, *, now_host, now_iso):
    from sniper.persistence import db_memecoin as db
    from sniper.services import memecoin_manual_intents as intents
    result = evaluate_candidate(proof, now_host=now_host, now_iso=now_iso)
    if not result['eligible']:
        raise ValueError(result['reason'])
    request = result['request']
    frozen = db.known_group_candidate_request(conn, request['desk_metadata']['opportunity_id'])
    if frozen['desk_metadata']['known_group_proof']['full_source_sha256'] != hashlib.sha256(_json(proof).encode()).hexdigest():
        raise ValueError('known_group_candidate_payload_mismatch')
    reason = _admission_refusal(conn, frozen['desk_metadata'], mint=frozen['mint'], now_iso=now_iso)
    if reason:
        raise ValueError(reason)
    frozen['ttl_s'] = request['ttl_s']
    return intents.enqueue(conn, now_iso=now_iso, **frozen)


def _validated_sell(sell):
    from sniper.services.wallet_universe_sol_venues import decode_transaction
    if (sell.get('source') != 'raw_confirmed_tail' or sell.get('commitment') != 'confirmed'
            or type(sell.get('flow_id')) is not int or sell['flow_id'] <= 0 or not sell.get('raw_ref')):
        raise ValueError('known_group_sell_receipt_unproven')
    tx = sell['transaction']
    _identity(sell, tx)
    if sell['signature'] != tx['transaction']['signatures'][0]:
        raise ValueError('known_group_sell_signature_mismatch')
    receipt = _number(sell['received_at_host'])
    if type(tx.get('blockTime')) is not int or not 0 < tx['blockTime'] <= receipt:
        raise ValueError('known_group_sell_chain_clock_unknown')
    if not receipt <= _number(sell['decoder_ready_at_host']) <= _number(sell['committed_at_host']):
        raise ValueError('known_group_sell_clock_unknown')
    result = decode_transaction(tx, sell['mint'])
    swaps = [s for s in result.get('swaps',[]) if s.get('actor_verified') is True
            and s['actor'] == sell['owner'] and s['protocol'] == 'pumpswap'
            and int(s['mint_delta_atomic']) > 0 and int(s['quote']['amount']) < 0]
    if result['status'] != 'trade' or len(swaps) != 1:
        raise ValueError('known_group_sell_unproven')
    return receipt,swaps[0]


def _compact_sell(sell, swap):
    out = {key:sell[key] for key in ('signature','mint','owner','slot','tx_index','received_at_host',
        'decoder_ready_at_host','committed_at_host','source','commitment','flow_id','raw_ref')}
    out.update({key:sell[key] for key in ('committed_row_observed_at_host','capture_processed_at_host') if key in sell})
    out.update(version='known-group-sell-compact-v1',method='native_confirmed_pumpswap_sell_v1',
        pool=swap['pool'],mint_delta_atomic=swap['mint_delta_atomic'],quote=copy.deepcopy(swap['quote']),
        invocation=swap['invocation'],block_time=sell['transaction']['blockTime'],
        full_source_sha256=hashlib.sha256(_json(sell).encode()).hexdigest())
    out['raw_sha256'],out['raw_hash_kind'] = _raw_digest(sell,sell['transaction'])
    return out


def ingest_sell(conn, sell, *, now_host, now_iso):
    """A validated observed sale invalidates pending launches before publishing any close hint."""
    from sniper.persistence import db_memecoin as db
    try:
        receipt,swap = _validated_sell(sell)
        if not receipt <= _number(now_host) or sell['committed_at_host'] > now_host:
            return None
        return db.invalidate_known_group_candidates(conn, sell=_compact_sell(sell,swap), now_iso=now_iso)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None


def nominate_sell(conn, position_id, sell, *, now_host, now_iso):
    from sniper.persistence import db_memecoin as db
    try:
        receipt,swap = _validated_sell(sell)
        if not receipt <= _number(now_host) <= receipt + POLICY['sell_freshness_s'] or sell['committed_at_host'] > now_host:
            return None
        position = db.get_position(conn, position_id)
        compact = _compact_sell(sell,swap)
        payload = dict(position_id=position_id, sell=compact, decision_at_host=now_host,
                       decision_at_utc=now_iso)
        payload['entry_contract_hash'] = hashlib.sha256(position['funded_contract_json'].encode()).hexdigest()
        reason = _exit_refusal(position, payload)
        if reason:
            return None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None
    db.invalidate_known_group_candidates(conn,sell=compact,now_iso=now_iso)
    return db.enqueue_known_group_exit(conn, position_id=position_id, payload=payload, now_iso=now_iso)


def _exit_refusal(position, payload):
    try:
        if (not position or position.get('execution_mode') != 'real' or position.get('signal_source') != 'desk'
                or position['id'] != payload['position_id']
                or hashlib.sha256(position['funded_contract_json'].encode()).hexdigest() != payload['entry_contract_hash']):
            return 'known_group_exit_identity_mismatch'
        desk = json.loads(position['funded_contract_json'])['source']['desk']
        proof = desk['known_group_proof']
        creation = _candidate_identity(proof)
        sell = payload['sell']
        receipt = _number(sell['received_at_host'])
        if (sell['version'] != 'known-group-sell-compact-v1' or sell['method'] != 'native_confirmed_pumpswap_sell_v1'
                or sell['source'] != 'raw_confirmed_tail' or sell['commitment'] != 'confirmed'
                or not receipt <= _number(sell['decoder_ready_at_host']) <= _number(sell['committed_at_host'])
                or int(sell['mint_delta_atomic']) <= 0 or int(sell['quote']['amount']) >= 0):
            return 'known_group_exit_sell_unproven'
        if (desk['cohort_id'] != COHORT_ID or desk['known_group_policy_hash'] != POLICY_HASH
                or sell['mint'] != creation['mint'] or position['mint'] != sell['mint']
                or sell['owner'] not in {m['address'] for m in proof['members']}
                or sell['block_time'] < creation['block_time']
                or not proof['creation']['received_at_host'] <= receipt <= payload['decision_at_host'] <= receipt + POLICY['sell_freshness_s']
                or sell['committed_at_host'] > payload['decision_at_host']
                or (sell['slot'], sell['tx_index']) <= (creation['slot'], proof['creation']['tx_index'])):
            return 'known_group_exit_proof_invalid'
        return None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return 'known_group_exit_proof_invalid'

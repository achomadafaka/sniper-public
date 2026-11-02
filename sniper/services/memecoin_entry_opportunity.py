"""One durable first-buy opportunity and fresh head evidence for automatic Solana copies."""
from __future__ import annotations

import json
import logging
import time
import uuid

import base58

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

log = logging.getLogger(__name__)
DEFAULT_MAX_SLOT_DELTA = 1


def enabled():
    return bool(getattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', False))


def _key(value, size):
    try:
        return (isinstance(value, str) and len(base58.b58decode(value)) == size
                and base58.b58encode(base58.b58decode(value)).decode() == value)
    except (ValueError, TypeError):
        return False


def _uid(value):
    if not isinstance(value, str):
        return False
    if value.startswith('pump:'):
        return _key(value[5:], 32)
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def exceptions():
    """Only exact IDs and explicit typed rules; malformed configuration widens nothing."""
    try:
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError('duplicate entry exception key')
                out[key] = value
            return out
        rows = json.loads(getattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON', '{}'),
                          object_pairs_hook=unique)
        if not isinstance(rows, dict):
            return {}
        for uid, row in rows.items():
            if (not _uid(uid) or not isinstance(row, dict)
                    or set(row) != {'max_slot_delta', 'allow_later_buys'}
                    or type(row['max_slot_delta']) is not int
                    or not 0 <= row['max_slot_delta'] <= DEFAULT_MAX_SLOT_DELTA
                    or type(row['allow_later_buys']) is not bool):
                return {}
        return rows
    except (ValueError, TypeError, OverflowError):
        return {}


def rule_for(source_user_id):
    row = exceptions().get(source_user_id)
    return ({**row, 'exception': True} if row is not None else
            {'max_slot_delta': DEFAULT_MAX_SLOT_DELTA, 'allow_later_buys': False, 'exception': False})


def head():
    from sniper.services.memecoin_slot_signal import HEAD
    observed = HEAD.copy()
    return observed.get('slot'), observed.get('mono')


def snapshot():
    return dict(enabled=enabled(), first_buy_only=True,
                default_max_slot_delta=None,
                qualified_exception_ids=sorted(exceptions()), head_max_age_ms=500,
                deadline_basis=None, landing_deadline_guaranteed=False)


def evaluate(conn, *, source_user_id, mint, source_signature, source_slot,
             source_pre_atomic, observed_at, head_fn=head, clock=time.monotonic):
    """Claim before selection; a refused first buy never turns its successor into a first buy."""
    if not enabled():
        return None, None
    if not _uid(source_user_id) or not _key(mint, 32) or not _key(source_signature, 64):
        return None, 'source_buy_identity_unknown'
    if type(source_slot) is not int or not 0 < source_slot < 2**63:
        source_slot = None
    selected = rule_for(source_user_id)
    try:
        cohort = m.wallet_real_cohort_start(conn, source_user_id, 'solana', include_initial=True)
        if cohort is None:
            return None, 'source_buy_cohort_unknown'
        first = m.claim_first_source_buy(conn, source_user_id=source_user_id, mint=mint,
            source_signature=source_signature, source_slot=source_slot,
            cohort_start=cohort, observed_at=observed_at)
    except Exception:
        log.exception('source first-buy claim unavailable uid=%s mint=%s', source_user_id, mint)
        return None, 'source_buy_claim_unknown'
    if not selected['allow_later_buys']:
        if first['source_signature'] != source_signature:
            return None, 'first_source_buy_missed'
    if source_slot is None or type(first['candidate_source_slot']) is not int:
        return None, 'source_buy_slot_unknown'
    if first['candidate_source_slot'] != source_slot:
        return None, 'source_buy_slot_mismatch'
    if first['source_signature'] == source_signature:
        if type(first['source_slot']) is not int:
            return None, 'source_buy_slot_unknown'
        if first['source_slot'] != source_slot:
            return None, 'source_buy_slot_mismatch'
    if not selected['allow_later_buys']:
        if type(source_pre_atomic) is not int or source_pre_atomic < 0:
            return None, 'source_first_buy_unknown'
        if source_pre_atomic > 0:
            return None, 'source_already_holding'
    from sniper.execution.submission_transport import entry_slot_refusal
    why = entry_slot_refusal(None, head_fn, clock=clock)
    if why:
        return None, why
    return {**selected, 'source_signature': source_signature, 'source_slot': source_slot,
            'first_source_signature': first['source_signature'],
            'entry_max_slot': None, 'max_slot_delta': None}, None


def event_window(conn, ev, *, observed_at, clock=time.monotonic):
    """Shared feed/on-chain adapter; opaque relay IDs never become source signatures."""
    if not enabled():
        return None, None
    origin = str(getattr(ev, 'fomo_event_id', '') or '')
    signature = None
    for prefix in ('onchain:', 'slotlane:', 'swap:raybot:'):
        if origin.startswith(prefix):
            signature = origin[len(prefix):].split(':', 1)[0]
            break
    evidence = getattr(ev, 'source_evidence', None)
    from types import SimpleNamespace
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    # Independent feeds skip the ordinary proof gate; require the exact receipt before claiming.
    proof = getattr(ev, 'source_initiation', None)
    if proof is None and isinstance(evidence, dict):
        proof = evidence.get('source_initiation')
    probe = SimpleNamespace(signal_source='onchain_watch', fomo_event_id='onchain:'+str(signature),
        source_initiation=proof, source_user_id=getattr(ev, 'source_user_id', None),
        source_wallet=getattr(ev, 'source_wallet', None), mint=getattr(ev, 'mint', None),
        source_block_number=getattr(ev, 'source_block_number', None))
    reason = source_initiation_refusal(probe, 'solana')
    if reason:
        return None, reason
    pre = getattr(ev, 'source_pre_atomic', None)
    if pre is None and isinstance(evidence, dict):
        pre = evidence.get('source_pre_atomic')
    return evaluate(conn, source_user_id=getattr(ev, 'source_user_id', None),
        mint=getattr(ev, 'mint', None), source_signature=signature,
        source_slot=getattr(ev, 'source_block_number', None), source_pre_atomic=pre,
        observed_at=observed_at, clock=clock)

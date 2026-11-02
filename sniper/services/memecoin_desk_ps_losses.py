"""Bounded read-only loss and selected-pool coverage proof for PumpSwap paper inputs.

Legacy gaps stay global. Only a complete immutable batch receipt lets a new reader replace a
coalesced decoder gap with all its individual transaction receipts. No capture or funded calls.
"""
from __future__ import annotations

from functools import lru_cache

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from contextlib import closing, nullcontext

MAX_ROWS = 20000
DECODE_CAUSES = ('decode_loss', 'virtual_quote_unproven')
PROTOCOL = 1
EVENT_ENVELOPE_KIND = 'amm_event_account_absence'
EVENT_ENVELOPE_PROFILE = 'pinned-amm-event-bloom4096k4-v1'
SWAP_TX_ENVELOPE_PROFILE = 'swap-tx-account-keys-bloom4096k4-v1'   # any swap-only tx with complete keys
_EVENT_DOMAIN = b'sniper:amm-event-accounts:v1\0'
_AMM_IDL = 'b7d8c57a4d9c4dd0109a9ab893052352333252d4eedced10ac091d4d66cab89b'
SCHEMAS = {
    'ps_loss_scope': 'CREATE TABLE IF NOT EXISTS ps_loss_scope('
        'signature TEXT,loss_key TEXT,pool_keys_json TEXT,complete INTEGER,proof TEXT,decoder_version TEXT,known_at REAL,'
        'PRIMARY KEY(signature,loss_key)) WITHOUT ROWID',
    'ps_loss_attribution': 'CREATE TABLE IF NOT EXISTS ps_loss_attribution('
        'signature TEXT,cause TEXT,reason TEXT,scope TEXT,pools_json TEXT,invocations INTEGER,'
        'discriminator TEXT,event_length INTEGER,idl_sha256 TEXT,received_at REAL,slot INTEGER,protocol INTEGER,'
        'PRIMARY KEY(signature,cause)) WITHOUT ROWID',
    'ps_loss_event_envelopes': 'CREATE TABLE IF NOT EXISTS ps_loss_event_envelopes('
        'signature TEXT,cause TEXT,kind TEXT,profile TEXT,bits BLOB,key_count INTEGER,idl_sha256 TEXT,'
        'received_at REAL,known_at REAL,slot INTEGER,protocol INTEGER,proof_sha256 TEXT,'
        'PRIMARY KEY(signature,cause)) WITHOUT ROWID',
    'ps_loss_batches': 'CREATE TABLE IF NOT EXISTS ps_loss_batches('
        'gap_rowid INTEGER PRIMARY KEY,at REAL,cause TEXT,receipt_count INTEGER,protocol INTEGER,through_at REAL)',
    'ps_loss_batch_members': 'CREATE TABLE IF NOT EXISTS ps_loss_batch_members('
        'gap_rowid INTEGER,signature TEXT,cause TEXT,PRIMARY KEY(gap_rowid,signature,cause)) WITHOUT ROWID',
    'ps_sweep_events': 'CREATE TABLE IF NOT EXISTS ps_sweep_events('
        'signature TEXT,ordinal INTEGER,pool TEXT,base_mint TEXT,quote_mint TEXT,recipient TEXT,payer TEXT,amount TEXT,bucket INTEGER,'
        'event_time INTEGER,slot INTEGER,tx_index INTEGER,received_at REAL,recorder_version TEXT,'
        'PRIMARY KEY(signature,ordinal)) WITHOUT ROWID',
    'ps_pool_selection': 'CREATE TABLE IF NOT EXISTS ps_pool_selection('
        'pool TEXT,session TEXT,start_at REAL,end_at REAL,protocol INTEGER,'
        'PRIMARY KEY(pool,session,start_at)) WITHOUT ROWID',
    'ps_capture_intervals': 'CREATE TABLE IF NOT EXISTS ps_capture_intervals('
        'session TEXT,start_at REAL,through_at REAL,protocol INTEGER,PRIMARY KEY(session,start_at)) WITHOUT ROWID',
}
INDEXES = {'ps_loss_batches': ('CREATE INDEX IF NOT EXISTS ps_loss_batches_through ON ps_loss_batches(through_at)',),
           'ps_sweep_events': ('CREATE INDEX IF NOT EXISTS ps_sweep_events_pool_slot ON ps_sweep_events(pool,slot)',),
           'ps_pool_selection': ('CREATE INDEX IF NOT EXISTS ps_pool_selection_pool_start '
                                 'ON ps_pool_selection(pool,start_at)',
                                 'CREATE INDEX IF NOT EXISTS ps_pool_selection_session ON ps_pool_selection(session)',)}


class LossUnreadable(Exception):
    pass


@lru_cache(maxsize=32768)
def _event_account_positions(key):
    from solders.pubkey import Pubkey
    if isinstance(key, str):
        raw = bytes(Pubkey.from_string(key))
    elif isinstance(key, bytes) and len(key) == 32:
        raw = bytes(Pubkey.from_bytes(key))
    else:
        raise ValueError('event account key')
    digest = hashlib.sha256(_EVENT_DOMAIN + raw).digest()
    return tuple(int.from_bytes(digest[i:i+2], 'little') & 4095 for i in (0, 2, 4, 6))


def event_account_filter(keys):
    """All complete resolved keys, compressed conservatively; no prices or writable-state proof."""
    if not isinstance(keys, (list, tuple)) or not 1 <= len(keys) <= 256:
        raise ValueError('event account count')
    bits = bytearray(512)
    for key in keys:
        for i in _event_account_positions(key):
            bits[i // 8] |= 1 << (i % 8)
    return bytes(bits)


def event_account_absent(bits, pool):
    """Only a zero target bit proves absence; collisions retain the original loss."""
    if not isinstance(bits, bytes) or len(bits) != 512:
        raise ValueError('event account filter')
    return any(not bits[i // 8] & (1 << (i % 8)) for i in _event_account_positions(pool))


def event_envelope_sha256(record):
    """Corruption check over the complete record; producer provenance remains independently required."""
    sig, cause, kind, profile, bits, count, idl, received, known, slot, protocol = record
    encoded = json.dumps([sig, cause, kind, profile, bits.hex(), count, idl,
                          float(received), float(known), slot, protocol],
                         separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _finite(v):
    try:
        return type(v) in (int, float) and math.isfinite(v)
    except OverflowError:
        return False



def pool_key_record(keys, *, signature, cause, received_at, known_at, slot):
    """New immutable membership receipt. The checksum detects corruption, not writer identity."""
    body = dict(keys=sorted(set(keys)), complete=True, proof='multi_hop_legs_v1', signature=signature,
                cause=cause, received_at=received_at, known_at=known_at, slot=slot,
                network_id='solana', venue='pumpswap', idl_sha256=_AMM_IDL, protocol=PROTOCOL, decoder_version='ps-routed-scope-v1')
    body['sha256'] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return body


@lru_cache(maxsize=32768)
def _pool_key_scope(encoded, sig, cause, at, slot, original, metadata):
    try:
        from solders.pubkey import Pubkey
        row = json.loads(encoded)
        fields = {'keys','complete','proof','signature','cause','received_at','known_at','slot',
                  'network_id','venue','idl_sha256','protocol','sha256','decoder_version'}
        valid = (isinstance(row, dict) and set(row) == fields and type(row['complete']) is bool
                 and row['proof'] == 'multi_hop_legs_v1' and row['signature'] == sig and row['cause'] == cause
                 and _finite(row['received_at']) and row['received_at'] == at
                 and type(row['slot']) is int and row['slot'] == slot
                 and row['network_id'] == 'solana' and row['venue'] == 'pumpswap'
                 and row['idl_sha256'] == _AMM_IDL and type(row['protocol']) is int and row['protocol'] == PROTOCOL
                 and row['decoder_version'] == 'ps-routed-scope-v1'
                 and metadata == (int(row['complete']),row['proof'],row['decoder_version'],row['known_at'])
                 and original == (_AMM_IDL, at, slot, PROTOCOL)
                 and _finite(row['known_at']) and row['known_at'] >= at
                 and isinstance(row['keys'], list) and 1 <= len(row['keys']) <= 32
                 and row['keys'] == sorted(set(row['keys']))
                 and all(isinstance(k, str) and str(Pubkey.from_string(k)) == k for k in row['keys']))
        if valid:
            body = {k:v for k,v in row.items() if k != 'sha256'}
            valid = row['sha256'] == hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, KeyError, OverflowError):
        valid = False
    if not valid:
        raise LossUnreadable('loss:malformed_pool_keys')
    return (frozenset(row['keys']) if row['complete'] else None), row['known_at']


def _bounds(lo, hi, max_rows, cut_slot=None):
    if not all(_finite(v) for v in (lo, hi)) or not 0 <= lo <= hi:
        raise LossUnreadable('loss:clock')
    if type(max_rows) is not int or not 0 < max_rows <= MAX_ROWS:
        raise LossUnreadable('loss:row_limit')
    if cut_slot is not None and (type(cut_slot) is not int or cut_slot < 0):
        raise LossUnreadable('loss:cut_slot')


def _open(desk_dir):
    path = Path(desk_dir) / 'pumpswap' / 'tape.db'
    if not path.is_file():
        raise LossUnreadable('loss:schema_missing')
    c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)
    c.execute('BEGIN')
    return c


def _index(c, table, name, prefix):
    columns = tuple(r[2] for r in c.execute('PRAGMA index_info(' + name + ')'))
    if columns[:len(prefix)] != prefix or not any(r[1] == name for r in c.execute('PRAGMA index_list(' + table + ')')):
        raise LossUnreadable('loss:index_missing:' + name)


def _primary(c, table, expected):
    keys = sorted((r[5], r[1]) for r in c.execute('PRAGMA table_info(' + table + ')') if r[5])
    if tuple(name for _, name in keys) != expected:
        raise LossUnreadable('loss:index_missing:' + table)


def _attribution(c, sig, cause, at, slot, tables):
    if 'ps_loss_attribution' not in tables:
        return None
    row = c.execute('SELECT reason,scope,pools_json,invocations,discriminator,event_length,idl_sha256,'
                    'received_at,slot,protocol FROM ps_loss_attribution WHERE signature=? AND cause=?',
                    (sig, cause)).fetchone()
    if row is None:  # pre-protocol receipts remain global
        return None
    return _attribution_scope(row, at, slot)


@lru_cache(maxsize=32768)
def _attribution_scope(row, at, slot):
    """Cache pure validation by full proof content, never receipt ID or database connection."""
    reason, scope, encoded, count, disc, length, idl, rx, sl, protocol = row
    try:
        pools = json.loads(encoded)
        from solders.pubkey import Pubkey
        valid = (isinstance(reason, str) and 0 < len(reason) <= 160 and scope in ('complete', 'unknown')
                 and isinstance(pools, list) and len(pools) <= 32 and pools == sorted(set(pools))
                 and all(isinstance(p, str) and str(Pubkey.from_string(p)) == p for p in pools)
                 and type(count) is int and 0 <= count <= 128 and (scope != 'complete' or (pools and count))
                 and isinstance(disc, str) and len(disc) <= 16 and all(x in '0123456789abcdef' for x in disc)
                 and (length is None or type(length) is int and length >= 0)
                 and idl == _AMM_IDL and rx == at and sl == slot and protocol == PROTOCOL)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise LossUnreadable('loss:malformed_attribution')
    return frozenset(pools) if scope == 'complete' else None


def _event_envelope(c, sig, cause, at, slot, tables):
    if 'ps_loss_event_envelopes' not in tables:
        return None
    row = c.execute('SELECT kind,profile,bits,key_count,idl_sha256,received_at,known_at,slot,protocol,proof_sha256 '
                    'FROM ps_loss_event_envelopes WHERE signature=? AND cause=?', (sig, cause)).fetchone()
    if row is None:
        return None
    original = (c.execute('SELECT invocations,received_at,slot,idl_sha256,protocol FROM ps_loss_attribution '
                          'WHERE signature=? AND cause=?', (sig, cause)).fetchone()
                if 'ps_loss_attribution' in tables else None)
    return _validated_envelope(row, original, sig, cause, at, slot)


@lru_cache(maxsize=32768)
def _validated_envelope(row, original, sig, cause, at, slot):
    kind, profile, bits, count, idl, rx, known, sl, protocol, checksum = row
    valid = (kind == EVENT_ENVELOPE_KIND and profile in (EVENT_ENVELOPE_PROFILE, SWAP_TX_ENVELOPE_PROFILE)
             and isinstance(bits, bytes) and len(bits) == 512 and any(bits)
             and type(count) is int and 1 <= count <= 256
             and sum(b.bit_count() for b in bits) <= 4 * count
             and idl == _AMM_IDL and rx == at and sl == slot and protocol == PROTOCOL
             and _finite(known) and known >= at
             and original is not None and type(original[0]) is int and 1 <= original[0] <= 128
             and original[1:] == (at, slot, _AMM_IDL, PROTOCOL))
    if valid:
        valid = checksum == event_envelope_sha256((sig, cause, *row[:-1]))
    if not valid:
        raise LossUnreadable('loss:malformed_event_envelope')
    return bits, known


def _attested(c, gap_id, at, cause, tables):
    if not {'ps_loss_batches', 'ps_loss_batch_members', 'ps_loss_attribution'} <= tables:
        return False
    batch = c.execute('SELECT at,cause,receipt_count,protocol,through_at FROM ps_loss_batches WHERE gap_rowid=?', (gap_id,)).fetchone()
    if batch is None:
        return False
    ba, bc, count, protocol, through = batch
    if (ba != at or bc != cause or protocol != PROTOCOL or type(count) is not int or count <= 0
            or not _finite(through) or through < at):
        raise LossUnreadable('loss:malformed_batch')
    # Validate the whole immutable batch without charging proven foreign members to a target's cap.
    rows = c.execute('SELECT m.signature,m.cause,l.received_at,l.slot FROM ps_loss_batch_members m '
                     'LEFT JOIN ps_decode_losses l ON l.signature=m.signature AND l.cause=m.cause '
                     'WHERE m.gap_rowid=?', (gap_id,))
    seen, earliest, complete = 0, None, True
    for sig, ca, rx, sl in rows:
        seen += 1
        if ca != cause or not _finite(rx) or not at <= rx <= through:
            complete = False
            continue
        earliest = rx if earliest is None else min(earliest, rx)
        if c.execute('SELECT 1 FROM ps_loss_attribution WHERE signature=? AND cause=?', (sig, ca)).fetchone() is None:
            complete = False
            continue
        _attribution(c, sig, ca, rx, sl, tables)
        _event_envelope(c, sig, ca, rx, sl, tables)
    return complete and seen == count and earliest == at


def losses(desk_dir, pool, lo, hi, *, cut_slot=None, max_rows=MAX_ROWS, pending=None, scoped=True, connection=None, loss_reads=None, receipt_details=None):
    """Loss receipt times in [lo,hi). Only proven other-pool losses may be excluded.

    pending is the worker's cause->receipt map. Future pending receipts do not contaminate a historical cut;
    its first receipt can hide later losses, so an earlier pending receipt invalidates from lo. An unparseable clock refuses. scoped=False preserves the old cohort's global decoder interpretation.
    """
    _bounds(lo, hi, max_rows, cut_slot)
    # The caller owns this short-lived set; do not share results across sheets or live connections.
    memo_key = None
    if loss_reads is not None and pending is None and connection is None:
        memo_key = (str(Path(desk_dir).absolute()), pool, lo, hi, cut_slot, max_rows, scoped)
        if memo_key in loss_reads:
            if receipt_details is not None:
                receipt_details.extend(loss_reads[("receipt_details",memo_key)])
            return list(loss_reads[memo_key])
    out, details = [], []
    try:
        with (closing(_open(desk_dir)) if connection is None else nullcontext(connection)) as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'gaps_ps', 'ps_decode_losses'} <= tables:
                raise LossUnreadable('loss:schema_missing')
            _index(c, 'gaps_ps', 'gaps_ps_at', ('at',))
            _primary(c, 'ps_decode_losses', ('signature', 'cause'))
            for table, key in [('ps_loss_attribution', ('signature','cause')),
                               ('ps_loss_batches', ('gap_rowid',)),
                               ('ps_loss_batch_members', ('gap_rowid','signature','cause')),
                               ('ps_loss_scope', ('signature','loss_key'))]:
                if table in tables:
                    _primary(c, table, key)
            if scoped and 'ps_loss_event_envelopes' in tables:
                _primary(c, 'ps_loss_event_envelopes', ('signature','cause'))
            _index(c, 'ps_decode_losses', 'ps_decode_losses_received', ('received_at',))
            legacy_pool = 'pool' if not scoped and any(r[1] == 'pool' for r in c.execute('PRAGMA table_info(gaps_ps)')) else 'NULL'
            scope_join = (' LEFT JOIN ps_loss_scope s ON s.signature=l.signature AND s.loss_key=l.cause'
                          if scoped and 'ps_loss_scope' in tables else '')
            scope_columns = ('s.pool_keys_json,s.complete,s.proof,s.decoder_version,s.known_at'
                             if scope_join else 'NULL,NULL,NULL,NULL,NULL')
            errors, classifications = [], {}
            def relevant(sig, cause, at, slot, encoded, complete, proof, decoder, known_at):
                try:
                    if (not isinstance(sig, str) or not sig or cause not in DECODE_CAUSES or not _finite(at)
                            or type(slot) is not int or slot < 0):
                        raise LossUnreadable('loss:malformed_receipt')
                    scope = _attribution(c, sig, cause, at, slot, tables) if scoped else None
                    envelope = _event_envelope(c, sig, cause, at, slot, tables) if scoped else None
                    if scoped and encoded is not None:
                        original = c.execute('SELECT idl_sha256,received_at,slot,protocol FROM ps_loss_attribution '
                                             'WHERE signature=? AND cause=?', (sig, cause)).fetchone()
                        routed, known = _pool_key_scope(encoded, sig, cause, at, slot, original, (complete,proof,decoder,known_at))
                        if routed is not None and known <= hi:
                            if scope is not None and scope != routed:
                                raise LossUnreadable('loss:conflicting_pool_keys')
                            scope = routed
                    if scope is None and envelope is not None and envelope[1] <= hi:
                        try:
                            if event_account_absent(envelope[0], pool):
                                return False
                        except (ValueError, TypeError):
                            raise LossUnreadable('loss:event_pool') from None
                    applies = scope is None or pool in scope
                    if applies:
                        classifications[(sig,cause)] = 'unknown_loss_receipts' if scope is None else 'own_pool_losses'
                    return applies
                except LossUnreadable as exc:
                    if not errors:
                        errors.append(exc)
                    return True

            def gap_relevant(gid, at, cause, legacy_scope):
                try:
                    if (not scoped and cause in DECODE_CAUSES and isinstance(legacy_scope, str)
                            and legacy_scope.strip() and legacy_scope != pool):
                        return False
                    return (not scoped or cause not in DECODE_CAUSES or not _finite(at)
                            or not _attested(c, gid, at, cause, tables))
                except LossUnreadable as exc:
                    if not errors:
                        errors.append(exc)
                    return True

            # Scope is proven before LIMIT; unknown/legacy losses still apply to every PS pool.
            c.create_function('sniper_ps_loss_relevant', 9, relevant)
            c.create_function('sniper_ps_gap_relevant', 4, gap_relevant)
            try:
                individual = c.execute('SELECT l.signature,l.cause,l.received_at,l.slot FROM ps_decode_losses l '
                                       'INDEXED BY ps_decode_losses_received' + scope_join +
                                       ' WHERE (l.received_at IS NULL OR (l.received_at>=? AND l.received_at<?)) '
                                       'AND sniper_ps_loss_relevant(l.signature,l.cause,l.received_at,l.slot,' +
                                       scope_columns + ') LIMIT ?', (lo, hi, max_rows + 1)).fetchall()
                if len(individual) > max_rows:
                    raise LossUnreadable('loss:row_limit')
                gaps = c.execute('SELECT rowid,at,cause,' + legacy_pool + ' FROM gaps_ps INDEXED BY gaps_ps_at '
                                 'WHERE (at IS NULL OR (at>=? AND at<?)) '
                                 'AND sniper_ps_gap_relevant(rowid,at,cause,' + legacy_pool + ') LIMIT ?',
                                 (lo, hi, max_rows + 1)).fetchall()
                if scoped and 'ps_loss_batches' in tables:
                    _index(c, 'ps_loss_batches', 'ps_loss_batches_through', ('through_at',))
                    overlap = c.execute('SELECT b.gap_rowid,g.at,g.cause,NULL FROM ps_loss_batches b '
                                        'INDEXED BY ps_loss_batches_through LEFT JOIN gaps_ps g ON g.rowid=b.gap_rowid '
                                        'WHERE b.through_at>=? AND b.at<? '
                                        'AND sniper_ps_gap_relevant(b.gap_rowid,g.at,g.cause,NULL) LIMIT ?',
                                        (lo, lo, max_rows + 1)).fetchall()
                    gaps.extend(overlap)
            finally:
                c.create_function('sniper_ps_loss_relevant', 9, None)
                c.create_function('sniper_ps_gap_relevant', 4, None)
            if errors:
                raise errors[0]
            if len(gaps) + len(individual) > max_rows:
                raise LossUnreadable('loss:row_limit')
            for gid, at, cause, legacy_scope in gaps:
                if not _finite(at) or not isinstance(cause, str) or not cause:
                    raise LossUnreadable('loss:malformed_gap')
                out.append(max(lo, at))
                details.append((('gap',gid),max(lo,at),'global_loss_receipts'))
            details.extend(((sig,cause),at,classifications[(sig,cause)]) for sig,cause,at,_ in individual)
            details = list(dict.fromkeys(details))
            out.extend(at for _, _, at, _ in individual)
        if pending is not None:
            if not isinstance(pending, dict) or len(pending) > 128:
                raise LossUnreadable('loss:pending_malformed')
            for cause, at in pending.items():
                if not isinstance(cause, str) or not cause or not _finite(at):
                    raise LossUnreadable('loss:pending_malformed')
                if at < hi:
                    out.append(max(lo, at))
    except sqlite3.Error as exc:
        raise LossUnreadable('loss:schema_or_read:' + type(exc).__name__) from None
    result = sorted(set(out))
    if memo_key is not None:
        loss_reads[memo_key] = tuple(result)
        loss_reads[('receipt_details',memo_key)] = tuple(details)
    if receipt_details is not None:
        receipt_details.extend(details)
    return result


def pool_coverage(desk_dir, pool, lo, hi, *, max_rows=MAX_ROWS, cut_slot=None, loss_reads=None):
    """Actual intake selection intersected with committed capture, as bounded host-time intervals.

    No rows before protocol startup are invented. A loss keeps only the continuous suffix of an interval.
    """
    _bounds(lo, hi, max_rows, cut_slot)
    try:
        with closing(_open(desk_dir)) as c:
            _index(c, 'ps_pool_selection', 'ps_pool_selection_pool_start', ('pool', 'start_at'))
            _primary(c, 'ps_capture_intervals', ('session','start_at'))
            selected = c.execute('SELECT session,start_at,end_at,protocol FROM ps_pool_selection '
                                 'INDEXED BY ps_pool_selection_pool_start WHERE pool=? AND start_at<? LIMIT ?',
                                 (pool, hi, max_rows + 1)).fetchall()
            if len(selected) > max_rows:
                raise LossUnreadable('coverage:row_limit')
            intervals = []
            budget = len(selected)
            for session, start, end, protocol in selected:
                if (not isinstance(session, str) or not session or not _finite(start)
                        or end is not None and (not _finite(end) or end < start) or protocol != PROTOCOL):
                    raise LossUnreadable('coverage:malformed')
                captures = c.execute('SELECT start_at,through_at,protocol FROM ps_capture_intervals '
                                     'WHERE session=? AND start_at<? AND through_at>? LIMIT ?',
                                     (session, hi, lo, max_rows + 1 - budget)).fetchall()
                budget += len(captures)
                if budget > max_rows:
                    raise LossUnreadable('coverage:row_limit')
                for a, b, version in captures:
                    if not all(_finite(v) for v in (a, b)) or b < a or version != PROTOCOL:
                        raise LossUnreadable('coverage:malformed')
                    left, right = max(lo, start, a), min(hi, b, end if end is not None else hi)
                    if left < right:
                        intervals.append((left, right))
    except (sqlite3.Error, LossUnreadable) as exc:
        if isinstance(exc, LossUnreadable) and str(exc).startswith('coverage:'):
            raise
        raise LossUnreadable('coverage:schema_or_index:' + type(exc).__name__) from None
    union = []
    for a, b in sorted(intervals):
        if union and a <= union[-1][1]:
            union[-1] = (union[-1][0], max(b, union[-1][1]))
        else:
            union.append((a, b))
    lost = losses(desk_dir, pool, lo, hi, max_rows=max_rows, cut_slot=cut_slot, loss_reads=loss_reads)
    return [(math.nextafter(max([a] + [x for x in lost if a <= x < b]), math.inf) if any(a <= x < b for x in lost) else a, b)
            for a, b in union]

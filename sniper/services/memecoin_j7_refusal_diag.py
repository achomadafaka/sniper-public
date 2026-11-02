"""J7 receipt refusal diagnostics (spec 2026-10-29-j7-refusal-diagnostics-design.md, option a).

Off the funded process (Alpha ruling 2026-10-29): a nice/idle-IO cron job runs this module once a
minute, samples a bounded window over a short-lived read-only connection and writes one small
snapshot file atomically. The runner heartbeat only copies that file; absent or stale is shown. Diagnostics only: nothing here admits, requeues or re-evaluates an attempt.
"""
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 1
CADENCE_S = 60
WINDOW_H = 24
LIMIT = 500
BUDGET_S = 0.25
STATEMENT_S = 0.025
STALE_S = 180
EXAMPLES = 20
MAX_BYTES = 65536
_SIG = re.compile(r'[1-9A-HJ-NP-Za-km-z]{64,88}')
_ADDR = re.compile(r'[0-9a-z]{32,44}')   # J7 swap ids carry wallet and mint lowercased
RECEIPT_STATES = ('own_row_late', 'own_row_absent', 'own_row_present_timing_unknown',
                  'identity_mismatch', 'unknown')
ELIGIBILITY = ('armed_on_chain', 'not_armed_on_chain', 'unknown')

_ATTEMPTS = ("SELECT attempt_id, origin_event_id, source_user_id, mint, decision_started_at, "
             "terminal_reason FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_attempt_source "
             "WHERE signal_source = 'follow_activity' AND decision_started_at >= ? "
             "AND decision_started_at <= ? AND network_id = 'solana' AND delivery_lane = 'j7_wallets' "
             "AND side = 'buy' AND terminal_status = 'refused' "
             "ORDER BY decision_started_at DESC LIMIT ?")


def _aware(value):
    try:
        out = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    return out if out.utcoffset() is not None else None


def exact_signature(origin_event_id):
    """(signature, wallet, mint) from an exact J7 swap id, same rules as memecoin_j7_backup.prepare.
    None for opaque/malformed ids: no lookup by wallet/mint/time proximity."""
    parts = str(origin_event_id or '').split(':')
    if (len(parts) != 5 or parts[0] != 'swap' or not parts[1] or not _SIG.fullmatch(parts[2])
            or not _ADDR.fullmatch(parts[3]) or not _ADDR.fullmatch(parts[4])):
        return None
    return parts[2], parts[3], parts[4]


def receipt_state(row, attempt, ident, sampled_at):
    """(state, extras) for one j7_receipt_not_observed refusal. `row` is the detection or None."""
    if ident is None:
        return 'unknown', {'reason': 'opaque_or_malformed_origin'}
    if row is None:
        return 'own_row_absent', {}
    wallet, mint = ident[1], ident[2]
    if (row['network_id'] != 'solana' or row['mint'] != attempt['mint']
            or row['source_user_id'] != attempt['source_user_id']
            or str(row['address']).lower() != wallet or str(row['mint']).lower() != mint):
        return 'identity_mismatch', {}
    extras = {'retracted': row['source_observation_state'] == 'retracted',
              'block_time_present': type(row['block_time']) is int and row['block_time'] > 0}
    decided, created = _aware(attempt['decision_started_at']), _aware(row['created_at'])
    if decided is None or created is None or created > sampled_at:
        return 'unknown', dict(extras, reason='invalid_timestamp')
    detected = _aware(row['detected_at'])
    if detected is None or detected > sampled_at:
        return 'unknown', dict(extras, reason='invalid_timestamp')
    extras['detected_lag_ms'] = round((detected - decided).total_seconds() * 1000, 1)
    if created > decided:
        return 'own_row_late', dict(extras, write_lag_ms=round((created - decided).total_seconds() * 1000, 1))
    return 'own_row_present_timing_unknown', extras


def eligibility(wallet_row):
    """Current-registry Solana eligibility; never decision-time. Missing profile, NULL/blank or
    malformed chains are unknown. Solana chains without approval, or paused/excluded, are not armed."""
    from sniper.persistence.db_memecoin import _parse_real_chains
    if wallet_row is None:
        return 'unknown'
    raw = wallet_row['real_chains']
    tokens = [t for t in str(raw or '').split(',') if t.strip()]
    chains = _parse_real_chains(raw)
    if not tokens or len(chains) != len({t.strip() for t in tokens}):
        return 'unknown'
    armed = ('solana' in chains and wallet_row['real_approved'] == 1
             and not wallet_row['paused'] and not wallet_row['excluded'])
    return 'armed_on_chain' if armed else 'not_armed_on_chain'


def sample(conn, now=None, *, limit=LIMIT, budget_s=BUDGET_S):
    """One complete bounded snapshot, or a typed incomplete one. Never a zero-filled partial."""
    sampled_at = now or datetime.now(timezone.utc)
    base = {'schema_version': SCHEMA_VERSION, 'sampled_at': sampled_at.isoformat(),
            'cohort_from': (sampled_at - timedelta(hours=WINDOW_H)).isoformat(),
            'cohort_to': sampled_at.isoformat(), 'limit': limit,
            'eligibility_basis': 'current_registry', 'eligibility_as_of': sampled_at.isoformat()}
    deadline = time.monotonic() + budget_s
    stmt = [deadline]
    conn.set_progress_handler(lambda: int(time.monotonic() > stmt[0]), 100)

    def run(sql, args):
        # Each statement gets STATEMENT_S of execution, never past the total budget.
        stmt[0] = min(deadline, time.monotonic() + STATEMENT_S)
        return conn.execute(sql, args)
    conn.row_factory = sqlite3.Row
    try:
        def check():
            if time.monotonic() > deadline:
                raise sqlite3.OperationalError('interrupted')
        check()
        attempts = run(_ATTEMPTS, (base['cohort_from'], base['cohort_to'], limit + 1)).fetchall()
        truncated = len(attempts) > limit
        attempts = attempts[:limit]
        reasons, receipts, cross, examples, signatures = {}, dict.fromkeys(RECEIPT_STATES, 0), {}, [], set()
        elig_counts, flags = dict.fromkeys(ELIGIBILITY, 0), {'paused': 0, 'excluded': 0, 'unapproved': 0}
        wallets = {}
        for attempt in attempts:
            check()
            reason = attempt['terminal_reason']
            reasons[reason] = reasons.get(reason, 0) + 1
            if reason != 'j7_receipt_not_observed':
                continue
            ident = exact_signature(attempt['origin_event_id'])
            row = None
            if ident is not None:
                signatures.add(ident[0])
                row = run('SELECT * FROM onchain_wallet_detections WHERE signature = ?',
                                   (ident[0],)).fetchone()
            state, extras = receipt_state(row, attempt, ident, sampled_at)
            uid = attempt['source_user_id']
            if uid not in wallets:
                wallets[uid] = run(
                    'SELECT real_chains, paused, excluded, real_approved FROM memecoin_wallets '
                    'WHERE source_user_id = ?', (uid,)).fetchone()
            wallet = wallets[uid]
            elig = eligibility(wallet)
            if wallet is not None:
                flags['paused'] += bool(wallet['paused'])
                flags['excluded'] += bool(wallet['excluded'])
                flags['unapproved'] += not wallet['real_approved']
            receipts[state] += 1
            elig_counts[elig] += 1
            cross[f'{elig}|{state}'] = cross.get(f'{elig}|{state}', 0) + 1
            if len(examples) < EXAMPLES:
                examples.append(dict(extras, attempt_id=attempt['attempt_id'], receipt_state=state,
                                     chain_eligibility=elig))
        check()
    except sqlite3.Error as exc:
        return dict(base, complete=False, error=type(exc).__name__ if 'interrupt' not in str(exc)
                    else 'budget_exhausted')
    finally:
        conn.set_progress_handler(None, 0)
    classified = sum(receipts.values())
    return dict(base, complete=True, error=None, truncated=truncated, selected_count=len(attempts),
                classified_count=classified, distinct_signatures=len(signatures),
                terminal_reasons=reasons, receipt_states=receipts, chain_eligibility=elig_counts,
                wallet_flags=flags, cross_tab=cross, examples=examples)


def snapshot_path():
    from sniper.util.state_paths import state_path
    return state_path('data', 'j7_refusal_diag.json')


def run_once(ledger_path, out_path):
    """One cron pass: sample on a short-lived read-only connection, publish atomically."""
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.atomic import atomic_write_json
    conn = None
    try:
        conn = get_readonly_connection(str(ledger_path))
        conn.execute('PRAGMA busy_timeout=25')
        snap = sample(conn)
    except Exception as exc:
        snap = {'schema_version': SCHEMA_VERSION, 'complete': False, 'error': type(exc).__name__,
                'sampled_at': datetime.now(timezone.utc).isoformat()}
    finally:
        if conn is not None:
            conn.close()
    atomic_write_json(str(out_path), snap)
    return snap


def read_snapshot(path, now=None):
    """The prebuilt snapshot with age and stale flag. Absent/unreadable/stale is never healthy."""
    import json
    import os
    import stat
    now = now or datetime.now(timezone.utc)
    try:
        # Funded loop caller: never follow a link, never block on a FIFO, never read past MAX_BYTES.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError('not a bounded regular file')
            raw = os.read(fd, MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_BYTES:
            raise ValueError('oversize')
        snap = json.loads(raw)
        if not isinstance(snap, dict):
            raise ValueError('not an object')
    except FileNotFoundError:
        return {'state': 'absent', 'stale': True}
    except (OSError, ValueError):
        return {'state': 'unreadable', 'stale': True}
    sampled = _aware(snap.get('sampled_at'))
    age = None if sampled is None else (now - sampled).total_seconds()
    return dict(snap, age_s=None if age is None else round(age, 1),
                stale=age is None or not 0 <= age <= STALE_S)


HEARTBEAT_KEYS = ('schema_version', 'state', 'age_s', 'stale', 'complete', 'error',
                  'sampled_at', 'cohort_from', 'cohort_to', 'limit', 'truncated', 'selected_count',
                  'classified_count', 'distinct_signatures', 'eligibility_basis', 'eligibility_as_of',
                  'terminal_reasons', 'receipt_states', 'chain_eligibility', 'wallet_flags',
                  'cross_tab', 'examples')


if __name__ == '__main__':
    from sniper.util.state_paths import state_path
    run_once(state_path('sniper_tracker.db'), snapshot_path())

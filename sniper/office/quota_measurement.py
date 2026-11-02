"""Host-only quota evidence. Explicit bootstrap; no network or funded imports.

All four counters are stored; counted usage excludes cache reads, per the proof ruling. The host
verifies per-wake scope and owns admission serialization and crash receipts.
Standalone observations retain 30 days; call prune off the wake path.
"""
from datetime import datetime, timezone
import json
import math
import sqlite3

from .store import canonical_json, write_transaction
from .runtime_observations import _insert

COUNTERS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
RETENTION_S = 30 * 86400


def _number(value):
    return type(value) in (int, float) and 0 <= value <= 2**53 - 1 and math.isfinite(value)


def _count(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _id(value):
    return isinstance(value, str) and 1 <= len(value) <= 256 and all(ord(c) >= 32 for c in value)


def ensure_schema(conn):
    with write_transaction(conn):
        conn.execute('CREATE TABLE IF NOT EXISTS quota_wakes('
            'wake_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,account_alias TEXT NOT NULL,'
            'session_id TEXT NOT NULL,result_id TEXT,ended_at REAL NOT NULL,'
            'used INTEGER,state TEXT NOT NULL,payload_json TEXT NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS quota_wakes_actor_time ON quota_wakes(actor_id,ended_at)')
        conn.execute('CREATE TABLE IF NOT EXISTS quota_model_requests(request_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,account_alias TEXT NOT NULL,'
            'at REAL NOT NULL,route TEXT NOT NULL,counted INTEGER NOT NULL,outcome TEXT NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS quota_model_requests_account ON quota_model_requests(account_alias,at)')
        conn.execute('CREATE INDEX IF NOT EXISTS quota_wakes_result ON quota_wakes(account_alias,session_id,result_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS quota_wakes_time ON quota_wakes(ended_at)')
        conn.execute('CREATE TABLE IF NOT EXISTS quota_account_snapshots('
            'snapshot_id TEXT PRIMARY KEY,account_alias TEXT NOT NULL,observed_at REAL NOT NULL,'
            'payload_json TEXT NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS quota_snapshots_time ON quota_account_snapshots(observed_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS quota_snapshots_account_time ON quota_account_snapshots(account_alias,observed_at)')
        for table in ('quota_wakes', 'quota_account_snapshots'):
            conn.execute('CREATE TRIGGER IF NOT EXISTS '+table+'_no_update BEFORE UPDATE ON '+table+
                         " BEGIN SELECT RAISE(ABORT,'insert_only'); END")


def record_wake(conn, *, epoch_id, actor_id, generation, incarnation, session_id, wake_id,
                account_alias, outcome, counters, result_id, started_at, ended_at,
                per_wake_max=None, usage_scope='unknown', absolute_ceiling=200000, calibration=False,uncapped=False, sdk_version=None, model=None, model_requests=None):
    result_id = result_id if _id(result_id) else None
    if (not all(_id(v) for v in (epoch_id, actor_id, incarnation, session_id, wake_id, account_alias))
            or type(generation) is not int or generation < 0 or not _number(started_at)
            or not _number(ended_at) or ended_at < started_at
            or outcome not in ('ok', 'timeout', 'failed', 'auth_expired', 'budget_exhausted')
            or type(calibration) is not bool):
        raise ValueError('quota_identity_invalid')
    counts = {k: counters.get(k) if isinstance(counters, dict) and _count(counters.get(k)) else None
              for k in COUNTERS}
    used = sum(counts[k] for k in ('input_tokens','output_tokens','cache_creation_input_tokens')) if all(v is not None for v in counts.values()) else None
    reason = ('counters_unknown' if used is None else 'usage_scope_unknown' if usage_scope != 'per_wake' or not _id(sdk_version)
              else 'absolute_ceiling_unknown' if not _count(absolute_ceiling) or absolute_ceiling == 0
              else 'wake_cap_unknown' if not calibration and not uncapped and (not _number(per_wake_max) or per_wake_max == 0) else None)
    known = reason is None
    over_ceiling = used > absolute_ceiling if known else None
    over_cap = (not calibration and not uncapped and used > per_wake_max) if known else None
    if known:
        reason = 'absolute_ceiling_breach' if over_ceiling else 'wake_cap_breach' if over_cap else None
    doc = dict(epoch_id=epoch_id, actor_id=actor_id, generation=generation, incarnation=incarnation,
        session_id=session_id, wake_id=wake_id, account_alias=account_alias, outcome=outcome,
        counters=counts, result_id=result_id, started_at=started_at, ended_at=ended_at,
        per_wake_max=per_wake_max if _number(per_wake_max) else None,
        usage_scope='per_wake' if usage_scope == 'per_wake' else 'unknown',
        state='known' if known else 'unknown', used=used if known else None, reason=reason,
        over_cap=over_cap, over_ceiling=over_ceiling, calibration=calibration,
        absolute_ceiling=absolute_ceiling if _count(absolute_ceiling) else None,
        sdk_version=sdk_version if _id(sdk_version) else None, model=model, model_requests=model_requests)
    with write_transaction(conn):
        prior = conn.execute('SELECT payload_json FROM quota_wakes WHERE wake_id=?', (wake_id,)).fetchone()
        if prior:
            saved = json.loads(prior[0])
            # A duplicate-result receipt remains idempotent on exact host retry.
            candidate = dict(saved, **{k:doc[k] for k in ('state','used','reason','over_cap','over_ceiling')})
            if candidate != doc:
                raise ValueError('quota_identity_conflict')
            return saved
        if result_id is not None and conn.execute(
                'SELECT 1 FROM quota_wakes WHERE account_alias=? AND session_id=? AND result_id=? LIMIT 1',
                (account_alias, session_id, result_id)).fetchone():
            doc.update(state='unknown', used=None, reason='result_reused', over_cap=None, over_ceiling=None)
        row = dict(wake_id=wake_id, actor_id=actor_id, account_alias=account_alias, session_id=session_id,
                   result_id=result_id, ended_at=ended_at, used=doc['used'], state=doc['state'],
                   payload_json=canonical_json(doc).decode())
        _insert(conn, 'quota_wakes', row, key='wake_id', identity=tuple(row))
    return doc


def wake_budget(conn, *, actor_id, now, limit):
    result = dict(state='unknown', used=None, limit=limit if _count(limit) else None, exhausted=None)
    if not _id(actor_id) or not _number(now) or not _count(limit) or limit == 0:
        return dict(result, reason='budget_input_unknown')
    start = (now // 86400) * 86400
    try:
        rows = conn.execute('SELECT used,state,ended_at FROM quota_wakes '
            'WHERE actor_id=? AND ended_at>=? AND ended_at<? ORDER BY ended_at LIMIT 10001',
            (actor_id, start, start + 86400)).fetchall()
    except sqlite3.Error:
        return dict(result, reason='budget_read_failed')
    if len(rows) > 10000:
        return dict(result, reason='budget_read_capped')
    if any(state != 'known' or not _count(used) or ended > now for used, state, ended in rows):
        return dict(result, reason='wake_usage_unknown')
    used = sum(r[0] for r in rows)
    if not _count(used):
        return dict(result, reason='budget_sum_invalid')
    return dict(result, state='known', used=used, exhausted=used >= limit,
                reason='budget_exhausted' if used >= limit else None)


def _reset(value):
    if _number(value):
        return value
    if isinstance(value, str):
        try:
            at = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return at.timestamp() if at.tzinfo is not None and _number(at.timestamp()) else None
        except (ValueError, OverflowError):
            pass
    return None


def record_account_snapshot(conn, *, snapshot_id, provider, account_alias, observed_at, raw, error=None):
    if not _id(snapshot_id) or not _id(account_alias) or not _number(observed_at) or provider not in ('claude', 'codex'):
        raise ValueError('quota_snapshot_identity_invalid')
    windows, missing = {}, []
    def add(name, doc, percent, reset, duration=None):
        value = doc.get(percent) if isinstance(doc, dict) else None
        at = _reset(doc.get(reset)) if isinstance(doc, dict) else None
        if not _number(value) or value > 100 or at is None or at < observed_at:
            missing.append(name)
            return
        windows[name] = dict(used_percent=value, remaining_percent=100-value, resets_at=at,
                             window_minutes=duration if _number(duration) and duration > 0 else None)
    raw = raw if isinstance(raw, dict) else {}
    if provider == 'claude':
        for name, duration in (('five_hour', 300), ('seven_day', 10080)):
            add(name, raw.get(name), 'utilization', 'resets_at', duration)
    else:
        buckets = raw.get('rateLimitsByLimitId')
        if not isinstance(buckets, dict):
            buckets = {'codex': raw.get('rateLimits')}
        if len(buckets) > 32:
            buckets = {}
        for bucket, doc in buckets.items():
            if not _id(bucket):
                missing.append('invalid_bucket')
                continue
            if not isinstance(doc, dict):
                doc = {}
            for name in ('primary', 'secondary'):
                window = doc.get(name)
                add(bucket+':'+name, window, 'usedPercent', 'resetsAt',
                    window.get('windowDurationMins') if isinstance(window, dict) else None)
    doc = dict(snapshot_id=snapshot_id, provider=provider, account_alias=account_alias,
        observed_at=observed_at, state='unknown' if error is not None else
        'known' if windows and not missing else 'partial' if windows else 'unknown',
        windows=windows, unknown_windows=missing,
        ordinary_usage_allowed=raw.get('ordinaryUsageAllowed') if type(raw.get('ordinaryUsageAllowed')) is bool else None,
        reason=error if error in ('timeout', 'auth', 'rate_limited', 'transport', 'invalid', 'unavailable') else None)
    row = dict(snapshot_id=snapshot_id, account_alias=account_alias, observed_at=observed_at,
               payload_json=canonical_json(doc).decode())
    with write_transaction(conn):
        _insert(conn, 'quota_account_snapshots', row, key='snapshot_id', identity=tuple(row))
    return doc


def prune(conn, *, now, limit=1000):
    if not _number(now) or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('quota_retention_input_invalid')
    counts = {}
    with write_transaction(conn):
        for table, key, stamp, label in (
                ('quota_wakes', 'wake_id', 'ended_at', 'wakes'),
                ('quota_account_snapshots', 'snapshot_id', 'observed_at', 'snapshots')):
            counts[label] = conn.execute('DELETE FROM '+table+' WHERE '+key+' IN (SELECT '+key+
                ' FROM '+table+' WHERE '+stamp+'<? ORDER BY '+stamp+' LIMIT ?)',
                (now-RETENTION_S, limit)).rowcount
    return counts


def account_guard(conn, *, account_alias, now, max_age_s, five_hour_max=50, seven_day_max=80):
    """Head proof guard: percentages USED, one indexed read, no provider call."""
    result = dict(state='unknown', allowed=False)
    if (not _id(account_alias) or not _number(now) or not _number(max_age_s) or max_age_s == 0
            or not all(_number(v) and 0 < v <= 100 for v in (five_hour_max,seven_day_max))):
        return dict(result, reason='account_guard_input_unknown')
    try:
        # The newest reading with any window within max_age: a failed poll carries none, and the age limit still parks on a sustained failure.
        rows = conn.execute('SELECT observed_at,payload_json FROM quota_account_snapshots '
            'WHERE account_alias=? ORDER BY observed_at DESC LIMIT 20',(account_alias,)).fetchall()
        fresh = [r for r in rows if 0 <= now-r[0] <= max_age_s]
        if not fresh:
            return dict(result, reason='account_snapshot_stale_or_absent')
        row = next((r for r in fresh if json.loads(r[1]).get('windows')), fresh[0])   # a reading with any window is information, even partial
        doc=json.loads(row[1])
        if doc['provider'] != 'claude':
            return dict(result, reason='account_windows_unknown')
        windows=doc['windows']
        # A known window over its limit decides even when the other is unknown; the reason says which park this is.
        if any(_number((windows.get(n) or {}).get('used_percent')) and _number(windows[n].get('resets_at')) and windows[n]['resets_at']>now
               and windows[n]['used_percent']>=ceiling for n,ceiling in (('five_hour',five_hour_max),('seven_day',seven_day_max))):
            return dict(state='known',allowed=False,reason='account_guard_reached',snapshot_id=doc['snapshot_id'])
        if doc['state'] != 'known':
            return dict(result, reason='account_windows_unknown')
        blocked=False
        for name, ceiling in (('five_hour',five_hour_max),('seven_day',seven_day_max)):
            window=windows[name]
            if (not _number(window['used_percent']) or window['used_percent'] > 100
                    or not _number(window['resets_at']) or window['resets_at'] <= now):
                return dict(result, reason='account_window_expired_or_unknown')
            blocked |= window['used_percent'] >= ceiling
        return dict(state='known',allowed=not blocked,
                    reason='account_guard_reached' if blocked else None,snapshot_id=doc['snapshot_id'])
    except (sqlite3.Error,ValueError,KeyError,TypeError):
        return dict(result,reason='account_snapshot_read_failed')


def record_model_request(conn, *, request_id, actor_id, account_alias, at, route, counted, outcome):
    conn.execute('INSERT INTO quota_model_requests VALUES(?,?,?,?,?,?,?)',(request_id,actor_id,account_alias,at,route,counted,outcome))


def model_requests(conn, *, actor_id, since, until):
    """Forwarded model requests by one seat, native-shell calls included: the proxy writes them, not the harness. Raises on a failed read."""
    return conn.execute('SELECT count(*) FROM quota_model_requests WHERE actor_id=? AND counted=1 AND at>=? AND at<=?',(actor_id,since,until)).fetchone()[0]


def codex_account_guard(conn, *, account_alias, now, max_age_s, primary_max=50, secondary_max=80):
    """Codex's windows, same used-percent rules as account_guard: every bucket's primary and secondary window must be known, current and under its limit. Numbers mirror Alpha's 50/80."""
    result = dict(state='unknown', allowed=False)
    if (not _id(account_alias) or not _number(now) or not _number(max_age_s) or max_age_s == 0
            or not all(_number(v) and 0 < v <= 100 for v in (primary_max, secondary_max))):
        return dict(result, reason='account_guard_input_unknown')
    try:
        row = conn.execute('SELECT observed_at,payload_json FROM quota_account_snapshots '
            'WHERE account_alias=? ORDER BY observed_at DESC LIMIT 1',(account_alias,)).fetchone()
        if row is None or not 0 <= now-row[0] <= max_age_s:
            return dict(result, reason='account_snapshot_stale_or_absent')
        doc = json.loads(row[1])
        if doc['provider'] != 'codex' or doc['state'] != 'known' or not doc['windows']:
            return dict(result, reason='account_windows_unknown')
        blocked = False
        for name, window in doc['windows'].items():
            limit = primary_max if name.endswith(':primary') else secondary_max
            if (not _number(window['used_percent']) or window['used_percent'] > 100
                    or not _number(window['resets_at']) or window['resets_at'] <= now):
                return dict(result, reason='account_window_expired_or_unknown')
            blocked |= window['used_percent'] >= limit
        if doc.get('ordinary_usage_allowed') is False:
            blocked = True
        # Unreadable counts park through the except below: unmeasured spending must not read as allowed.
        counted = conn.execute('SELECT count(*) FROM quota_model_requests WHERE account_alias=? AND counted=1 AND at>?',(account_alias,now-3600)).fetchone()[0]
        return dict(state='known', allowed=not blocked,
                    reason='account_guard_reached' if blocked else None, snapshot_id=doc['snapshot_id'], model_requests_1h=counted)
    except (sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        return dict(result, reason='account_snapshot_read_failed')

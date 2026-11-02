"""Independent chain verification against externally trusted auditor head pins."""
import json
import os
import tempfile
import sqlite3
from pathlib import Path

from sniper.office.store import APPEND_ONLY_TABLES, verify_chain


def audit_chains(conn, pin_path, *, tables):
    """Pin only committed rows from one consistent read snapshot."""
    if conn.in_transaction:
        raise ValueError("audit_requires_own_read_transaction")
    conn.execute("BEGIN")
    try:
        return _audit_chains(conn, pin_path, tables=tables)
    finally:
        conn.rollback()


def _audit_chains(conn, pin_path, *, tables):
    """Never bootstrap trust from the database being checked. Caller owns paging."""
    tables = tuple(tables)
    if not tables or set(tables) - APPEND_ONLY_TABLES:
        raise ValueError('invalid_audit_tables')
    path = Path(pin_path)
    try:
        pins = json.loads(path.read_text())
    except FileNotFoundError:
        return {'state': 'unknown', 'breaches': ['missing_trusted_pin']}
    except (OSError, ValueError):
        return {'state': 'unknown', 'breaches': ['unreadable_trusted_pin']}
    if not isinstance(pins, dict):
        return {'state': 'unknown', 'breaches': ['invalid_trusted_pin']}
    breaches, heads = [], {}
    for table in tables:
        pin = pins.get(table)
        if (not isinstance(pin, list) or len(pin) != 2
                or type(pin[0]) is not int or pin[0] < 0
                or not isinstance(pin[1], str)):
            breaches.append(table + ':missing_trusted_pin')
            continue
        good, bad = verify_chain(conn, table)
        if not good:
            breaches.append(table + ':chain_invalid:' + str(bad))
            continue
        row = conn.execute('SELECT seq,row_hash FROM ' + table + ' ORDER BY seq DESC LIMIT 1').fetchone()
        head = list(row) if row else [0, '']
        old = conn.execute('SELECT row_hash FROM ' + table + ' WHERE seq=?', (pin[0],)).fetchone() if pin[0] else None
        if head[0] < pin[0] or (pin[0] and (old is None or old[0] != pin[1])) or (pin[0] == 0 and pin[1] != ''):
            breaches.append(table + ':deleted_tail')
        else:
            heads[table] = head
    if breaches:
        return {'state': 'breach', 'breaches': breaches}
    pins.update(heads)
    # Pins must live in an auditor-owned directory, outside supervisor authority.
    fd, name = tempfile.mkstemp(prefix='.pins-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(pins, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return {'state': 'clean', 'breaches': [], 'heads': heads}


COUNTERS = ('unauthorized_funded_writes', 'accepted_future_knowledge_reads',
            'altered_evidence_identities', 'duplicate_credited_fills', 'ledger_reconciliation_errors',
            'scorer_changes_inside_epoch', 'unlabelled_model_fallbacks', 'replay_while_live_backlogged')


def check_safety(counters, heartbeats, *, now, max_age_s):
    """Missing observations are unknown; all positive safety counters stop evaluation."""
    import math
    if not all(type(value) in (int, float) and math.isfinite(value) for value in (now, max_age_s)) or max_age_s <= 0:
        raise ValueError('invalid_audit_clock')
    breaches, unknown = [], []
    for name in COUNTERS:
        value = counters.get(name)
        if type(value) is not int or value < 0:
            unknown.append(name)
        elif value:
            breaches.append(name)
    for role in ('supervisor', 'head'):
        at = heartbeats.get(role)
        if type(at) not in (int, float) or not math.isfinite(at) or at > now:
            unknown.append(role + ':heartbeat_unknown')
        elif now - at > max_age_s:
            breaches.append(role + ':heartbeat_stale')
    return {'state': 'breach' if breaches else 'unknown' if unknown else 'clean',
            'breaches': breaches, 'unknown': unknown, 'counters': dict(counters)}


def reconcile_ledger(conn):
    """Compare order totals to independently recorded cashflows, per book, from one snapshot."""
    import math
    if conn.in_transaction:
        raise ValueError('reconciliation_requires_own_read_transaction')
    conn.execute('BEGIN')
    books, breaches = {}, []
    try:
        for (book_id,) in conn.execute('SELECT book_id FROM books ORDER BY book_id'):
            capital = conn.execute('SELECT COALESCE(SUM(usd),0) FROM capital_flows WHERE book_id=?', (book_id,)).fetchone()[0]
            cashflow = conn.execute('SELECT COALESCE(SUM(c.usd),0) FROM cashflows c JOIN orders o USING(order_id) WHERE o.book_id=?', (book_id,)).fetchone()[0]
            expected = capital
            for oid, state, filled, closed, spent, proceeds, costs in conn.execute('SELECT order_id,state,filled_at,closed_at,spend_usd,exit_usd,costs_usd FROM orders WHERE book_id=?', (book_id,)):
                values = [costs]
                if filled is not None:
                    values.append(spent)
                if closed is not None:
                    values.append(proceeds)
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    breaches.append(oid + ':unknown_cashflow')
                    continue
                counts = dict(conn.execute('SELECT kind,COUNT(*) FROM cashflows WHERE order_id=? GROUP BY kind', (oid,)))
                if counts != ({'entry': 1, 'exit': 1} if closed is not None else {'entry': 1} if filled is not None else {}):
                    breaches.append(oid + ':cashflow_identity_mismatch')
                if state == 'closed' and (filled is None or closed is None):
                    breaches.append(oid + ':closed_without_fill')
                expected += (proceeds if closed is not None else 0) - (spent if filled is not None else 0) - costs
            recorded = capital + cashflow
            difference = recorded - expected
            if not math.isfinite(difference) or abs(difference) > 1e-6:
                breaches.append(book_id + ':ledger_reconciliation_error')
            books[book_id] = dict(expected_cash_usd=expected, recorded_cash_usd=recorded, difference_usd=difference)
    finally:
        conn.rollback()
    return {'state': 'breach' if breaches else 'clean' if books else 'unknown', 'books': books, 'breaches': breaches or ([] if books else ['no_books'])}


def audit_once(office, evaluation, ledger, pin_dir, *, counters, heartbeats, now, max_age_s, page):
    """Auditor authority only. No model, supervisor mutation, or funded write capability."""
    from sniper.office.store import PRIVATE_TABLES
    checks = {}
    for name, conn, tables in (('office', office, APPEND_ONLY_TABLES - PRIVATE_TABLES),
                                ('evaluation', evaluation, PRIVATE_TABLES)):
        try:
            checks[name] = audit_chains(conn, Path(pin_dir) / (name + '.json'), tables=sorted(tables))
        except (OSError, ValueError, sqlite3.Error) as exc:
            checks[name] = {'state': 'unknown', 'breaches': [type(exc).__name__]}
    try:
        checks['reconciliation'] = reconcile_ledger(ledger)
    except (sqlite3.Error, ValueError) as exc:
        checks['reconciliation'] = {'state': 'unknown', 'breaches': [type(exc).__name__]}
    checks['safety'] = check_safety(counters, heartbeats, now=now, max_age_s=max_age_s)
    state = 'breach' if any(c['state'] == 'breach' for c in checks.values()) else 'unknown' if any(c['state'] != 'clean' for c in checks.values()) else 'clean'
    result = dict(state=state, at=now, checks=checks)
    if state != 'clean':
        reasons = [name + ':' + reason for name, check in checks.items() for reason in check.get('breaches', []) + check.get('unknown', [])]
        result['page_result'] = page(kind='office-audit', text='; '.join(reasons))
    return result

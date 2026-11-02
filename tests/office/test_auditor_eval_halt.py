"""The auditor's single write: a zero-tolerance halt of the eval-1 book on breach, nothing else."""
import sqlite3

from scripts import office_auditor as A


class Spy:
    def __init__(self, conn):self.conn, self.sql = conn, []
    def execute(self, sql, *a):self.sql.append(sql); return self.conn.execute(sql, *a)
    def close(self):self.conn.close()
    row_factory = property(lambda s: s.conn.row_factory, lambda s, v: setattr(s.conn, 'row_factory', v))


def test_breach_calls_only_the_halt_with_trusted_identity(tmp_path, monkeypatch):
    path = tmp_path / 'ledger.db'; sqlite3.connect(path).close()
    spies, calls = [], []
    real = sqlite3.connect
    def connect(target, **kw):
        spy = Spy(real(target, **kw)); spies.append((target, spy)); return spy
    monkeypatch.setattr(A.sqlite3, 'connect', connect)
    result = {'state': 'breach', 'checks': {'safety': {'breaches': ['duplicate_credited_fills']}, 'office': {'breaches': []}}}
    out = A.halt_on_breach(path, result, now=100., halt=lambda conn, **kw: calls.append((conn, kw)) or {'halt_kind': kw['kind']})
    assert out == {'halt_kind': 'zero_tolerance'} and len(spies) == 1 and spies[0][0].endswith('?mode=rw')
    conn, kw = calls[0]
    assert conn is spies[0][1] and kw == {'epoch_id': 'office-paper-eval-1', 'reason': 'safety:duplicate_credited_fills',
                                          'actor': 'office-auditor', 'now': 100., 'kind': 'zero_tolerance'}
    assert spies[0][1].sql == []                                  # the auditor itself issued no SQL through the handle


def test_clean_or_unknown_never_opens_a_write_handle(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(A, 'halt_on_breach', lambda *a, **k: opened.append(1))
    monkeypatch.setattr(A, 'audit_once', lambda *a, **k: {'state': 'unknown', 'checks': {}})
    monkeypatch.setattr(A, 'reconcile_ledger', lambda c: {'state': 'clean', 'breaches': []})
    for name in ('office.db', 'evaluation.db', 'ledger.db', 'null_ledger.db'):
        sqlite3.connect(tmp_path / name).execute('CREATE TABLE IF NOT EXISTS books(book_id TEXT)').connection.commit()
    obs = tmp_path / 'obs.json'; obs.write_text('{"at": 100}')
    A.run_once(tmp_path, tmp_path, obs, now=100., page=lambda **k: None)
    assert opened == []


def test_halt_reason_fits_the_ledger_api_bound(tmp_path):
    path = tmp_path / 'ledger.db'; sqlite3.connect(path).close()
    seen = []
    result = {'state': 'breach', 'checks': {'safety': {'breaches': ['x' * 400]}}}
    A.halt_on_breach(path, result, now=1., halt=lambda conn, **kw: seen.append(kw['reason']))
    assert 1 <= len(seen[0]) <= 160

import json
from sniper.office import store
from sniper.office.auditor import audit_chains


def test_pin_unknown_then_append_then_deleted_tail(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    pins = tmp_path / 'auditor' / 'pins.json'
    assert audit_chains(conn, pins, tables=['claims'])['state'] == 'unknown'
    assert not pins.exists()
    store.append(conn, 'claims', {'claim_hash': 'a'})
    head = conn.execute('SELECT seq,row_hash FROM claims').fetchone()
    pins.parent.mkdir()
    pins.write_text(json.dumps({'claims': list(head)}))
    store.append(conn, 'claims', {'claim_hash': 'b'})
    assert audit_chains(conn, pins, tables=['claims'])['state'] == 'clean'
    conn.execute('DROP TRIGGER claims_no_delete')
    conn.execute('DELETE FROM claims WHERE seq=2')
    conn.commit()
    assert audit_chains(conn, pins, tables=['claims'])['breaches'] == ['claims:deleted_tail']


def test_invalid_chain_does_not_advance_any_pin(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    for table in ('claims', 'receipts'):
        store.append(conn, table, {'value': 1})
    pins = tmp_path / 'pins.json'
    pins.write_text(json.dumps({table: list(conn.execute('SELECT seq,row_hash FROM '+table).fetchone()) for table in ('claims', 'receipts')}))
    before = pins.read_bytes()
    store.append(conn, 'claims', {'value': 2})
    conn.execute('DROP TRIGGER receipts_no_update')
    conn.execute("UPDATE receipts SET payload_json='{}'")
    conn.commit()
    assert audit_chains(conn, pins, tables=['claims', 'receipts'])['state'] == 'breach'
    assert pins.read_bytes() == before


def test_empty_trusted_genesis_advances(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    pins = tmp_path / 'pins.json'
    pins.write_text('{"claims":[0,""]}')
    assert audit_chains(conn, pins, tables=['claims'])['heads'] == {'claims': [0, '']}
    store.append(conn, 'claims', {'claim_hash': 'new'})
    assert audit_chains(conn, pins, tables=['claims'])['heads']['claims'][0] == 1


def test_uncommitted_rows_cannot_be_pinned(tmp_path):
    import pytest
    conn = store.open_store(tmp_path / 'office.db')
    conn.execute('BEGIN')
    with pytest.raises(ValueError, match='own_read_transaction'):
        audit_chains(conn, tmp_path / 'pins.json', tables=['claims'])
    assert conn.in_transaction
    conn.rollback()


def test_unknown_counter_is_not_zero_and_replay_backlog_breaches():
    from sniper.office.auditor import check_safety, COUNTERS
    assert check_safety({}, {'supervisor': 100., 'head': 100.}, now=101., max_age_s=30.)['state'] == 'unknown'
    counters = dict.fromkeys(COUNTERS, 0)
    counters['replay_while_live_backlogged'] = 1
    result = check_safety(counters, {'supervisor': 100., 'head': 100.}, now=101., max_age_s=30.)
    assert result['state'] == 'breach' and 'replay_while_live_backlogged' in result['breaches']
    result = check_safety(dict.fromkeys(COUNTERS, 0), {'supervisor': 1., 'head': 100.}, now=101., max_age_s=30.)
    assert 'supervisor:heartbeat_stale' in result['breaches']


from office.test_ledger import book, opened, policy


def test_reconciliation_error_uses_real_capital_and_cashflows(book):
    from sniper.office.auditor import reconcile_ledger
    conn, quotes, _ = book
    opened(conn, quotes, policy())
    assert reconcile_ledger(conn)['state'] == 'clean'
    conn.execute("UPDATE orders SET costs_usd=costs_usd+1 WHERE order_id='o1'")
    conn.commit()
    result = reconcile_ledger(conn)
    assert result['state'] == 'breach'
    assert abs(result['books']['A']['difference_usd']) == 1.


def test_reconciliation_error_and_dead_head_page(book, tmp_path):
    from sniper.office.auditor import audit_once, COUNTERS
    conn, quotes, _ = book
    opened(conn, quotes, policy())
    conn.execute("UPDATE orders SET costs_usd=costs_usd+1 WHERE order_id='o1'")
    conn.commit()
    public = store.open_store(tmp_path / 'office.db')
    private = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
    pages = []
    result = audit_once(public, private, conn, tmp_path / 'pins', counters=dict.fromkeys(COUNTERS, 0), heartbeats={'supervisor': 100., 'head': 1.}, now=101., max_age_s=30., page=lambda **kw: pages.append(kw))
    assert result['state'] == 'breach'
    assert pages[0]['kind'] == 'office-audit'
    assert 'heartbeat_stale' in pages[0]['text'] and 'ledger_reconciliation_error' in pages[0]['text']


def test_missing_audit_files_page_unknown_without_creating_database(tmp_path):
    from scripts.office_auditor import run_once
    pages = []
    out = run_once(tmp_path, tmp_path / 'pins', tmp_path / 'absent.json', now=100., page=lambda **kw: pages.append(kw))
    assert out['state'] == 'unknown' and pages[0]['kind'] == 'office-audit'
    assert not list(tmp_path.glob('*.db'))


def test_auditor_cli_uses_numeric_corrected_time(monkeypatch, tmp_path):
    from scripts import office_auditor as cli
    from sniper.services import memecoin_alerts
    monkeypatch.setattr('sys.argv', ['office_auditor.py', '--state-root', str(tmp_path), '--pin-dir', str(tmp_path/'pins'), '--observations', str(tmp_path/'observations.json')])
    monkeypatch.setattr(cli, 'corrected_now', lambda path, host_now: 123.)
    monkeypatch.setattr(memecoin_alerts, 'send_alert', lambda **kw: None)
    seen = []
    monkeypatch.setattr(cli, 'run_once', lambda *args, **kw: seen.append(kw['now']) or {'state': 'unknown'})
    assert cli.main() == 2
    assert seen == [123.]

import pytest
from sniper.office import capture, store


def test_opportunity_row_precedes_agent_access(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    with pytest.raises(ValueError, match='not_in_opportunity_log'):
        capture.require_opportunity(conn, 'epoch', 'solana', 'mint')
    row = dict(epoch_id='epoch', network_id='solana', mint='mint', first_eligible_at=100., shift_id='asia', sheet_hash='s', snapshot_hash='h', receipt_cut=100., slot_cut=12, knowledge_cut=100., clock_sample_id='clock', hard_filter_version='v1', eligible=True, reason='passed')
    capture.log_opportunity(conn, row)
    assert capture.require_opportunity(conn, 'epoch', 'solana', 'mint')['first_eligible_at'] == 100.
    capture.log_opportunity(conn, {**row, 'first_eligible_at': 101.})
    assert conn.execute('SELECT COUNT(*) FROM opportunity_log').fetchone()[0] == 1


def test_dead_source_is_a_gap_not_empty(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    capture.record_gap(conn, epoch_id='e', source='ps', from_at=1., to_at=2., state='off')
    row = conn.execute('SELECT payload_json FROM opportunity_gaps').fetchone()
    assert '"state":"off"' in row[0]


def test_unknown_or_future_opportunity_never_released(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    row = dict(epoch_id='epoch', network_id='solana', mint='mint', first_eligible_at=100., shift_id='asia', sheet_hash='s', snapshot_hash='h', receipt_cut=100., slot_cut=12, knowledge_cut=101., clock_sample_id='clock', hard_filter_version='v1', eligible=True, reason='passed')
    with pytest.raises(ValueError, match='future_opportunity_cut'):
        capture.log_opportunity(conn, row)
    with pytest.raises(ValueError, match='invalid_clock_sample_id'):
        capture.log_opportunity(conn, {**row, 'knowledge_cut': 100., 'clock_sample_id': None})
    assert conn.execute('SELECT COUNT(*) FROM opportunity_log').fetchone()[0] == 0


def test_coverage_requires_a_committed_successful_poll_receipt(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    with pytest.raises(ValueError, match='coverage_proof_missing'):
        capture.record_coverage(conn, epoch_id='e', source='feed', from_at=1., to_at=2., proof_hash='a'*64)
    proof = store.append(conn, 'receipts', dict(kind='eligible_source_poll', source='feed', state='observed', from_at=1., to_at=2., known_at=2.))
    capture.record_coverage(conn, epoch_id='e', source='feed', from_at=1., to_at=2., proof_hash=proof)
    assert conn.execute('SELECT COUNT(*) FROM opportunity_coverage').fetchone()[0] == 1
    with pytest.raises(ValueError, match='coverage_proof_mismatch'):
        capture.record_coverage(conn, epoch_id='e', source='feed', from_at=1., to_at=3., proof_hash=proof)

import pytest
from sniper.office import capture, store


def test_capture_stays_private_and_frozen_look_is_idempotent(tmp_path):
    public = store.open_store(tmp_path / 'office.db')
    private = store.open_store(tmp_path / 'evaluation.db', evaluation=True)
    observation = {'state': 'path_gap', 'rows': [], 'reason': 'coverage_unknown'}
    with pytest.raises(ValueError, match='private_evaluation_store_required'):
        capture.record_capture(public, candidate_seq=1, epoch_id='e', look_id='l', observation=observation)
    first = capture.record_capture(private, candidate_seq=1, epoch_id='e', look_id='l', observation=observation)
    assert capture.record_capture(private, candidate_seq=1, epoch_id='e', look_id='l', observation=observation) == first
    assert private.execute('SELECT COUNT(*) FROM outcome_captures').fetchone()[0] == 1
    with pytest.raises(ValueError, match='frozen_capture_changed'):
        capture.record_capture(private, candidate_seq=1, epoch_id='e', look_id='l', observation={'state': 'observed', 'rows': []})

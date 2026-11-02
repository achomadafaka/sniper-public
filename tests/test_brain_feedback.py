import pytest
from unittest.mock import patch, MagicMock
from brain import Brain


@pytest.fixture
def brain(tmp_path):
    db = str(tmp_path / "test.db")
    return Brain(db_path=db)


def test_maybe_update_brain_fires_at_10th_trade(brain):
    with patch.object(brain, '_do_brain_update') as mock:
        brain.maybe_update_brain("news_intel_v1", "politics", "win", 15.0, 10)
        mock.assert_called_once()


def test_maybe_update_brain_skips_non_multiple(brain):
    with patch.object(brain, '_do_brain_update') as mock:
        brain.maybe_update_brain("news_intel_v1", "politics", "win", 15.0, 7)
        mock.assert_not_called()


def test_maybe_update_brain_skips_zero(brain):
    with patch.object(brain, '_do_brain_update') as mock:
        brain.maybe_update_brain("news_intel_v1", "politics", "win", 15.0, 0)
        mock.assert_not_called()


def test_maybe_update_brain_survives_exception(brain):
    with patch.object(brain, '_do_brain_update', side_effect=Exception("boom")):
        brain.maybe_update_brain("news_intel_v1", "politics", "win", 15.0, 10)
        # No exception raised = pass

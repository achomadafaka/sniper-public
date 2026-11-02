"""thesis_stance — objective PnL bucket (underwater/flat/winning/unknown), NOT sentiment."""
from sniper.analysis.memecoin_thesis import thesis_stance


def test_stance_unknown_when_none():
    assert thesis_stance(None) == "unknown"
    assert thesis_stance("not-a-number") == "unknown"


def test_stance_underwater_at_or_below_threshold():
    assert thesis_stance(-49.4) == "underwater"
    assert thesis_stance(-10.0) == "underwater"     # boundary is inclusive (<=)


def test_stance_winning_at_or_above_threshold():
    assert thesis_stance(331.0) == "winning"
    assert thesis_stance(20.0) == "winning"         # boundary inclusive (>=)


def test_stance_flat_between():
    assert thesis_stance(0.0) == "flat"
    assert thesis_stance(10.1) == "flat"
    assert thesis_stance(-5.0) == "flat"

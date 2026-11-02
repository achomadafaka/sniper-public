from pathlib import Path

import pytest

from sniper.analysis.public_truth_stale.manual_log import load_manual_observations
from sniper.analysis.public_truth_stale.types import SourceLane


HEADER = (
    "signal_id,market_url,market_id,condition_id,token_id,outcome,rule_text_hash,"
    "resolution_source_url,source_lane,source_family,source_account_id,source_url,"
    "source_timestamp,fetch_timestamp,observed_source_value,rule_match_status,"
    "finality_status,confirmation_status,stake_usdc,modeled_cost_per_share,"
    "orderbook_file,notes\n"
)


def test_load_manual_observations_parses_valid_csv(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        HEADER
        + "s1,https://polymarket.com/event/x,m1,c1,t1,YES,sha256:abc,"
        + "https://official.example/final,resolution_authoritative,sports_official,,"
        + "https://official.example/final,2026-06-15T21:00:00Z,2026-06-15T21:00:10Z,"
        + "home_won,exact_match,official_final,still_unconfirmed,20,0.005,"
        + "examples/orderbook_signal_001.json,\n",
        encoding="utf-8",
    )

    observations = load_manual_observations(csv_path)

    assert len(observations) == 1
    assert observations[0].signal_id == "s1"
    assert observations[0].source_lane is SourceLane.RESOLUTION_AUTHORITATIVE
    assert observations[0].stake_usdc == pytest.approx(20.0)


def test_load_manual_observations_rejects_unknown_lane(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        HEADER
        + "s1,https://polymarket.com/event/x,m1,c1,t1,YES,sha256:abc,"
        + "https://official.example/final,bad_lane,sports_official,,"
        + "https://official.example/final,2026-06-15T21:00:00Z,2026-06-15T21:00:10Z,"
        + "home_won,exact_match,official_final,still_unconfirmed,20,0.005,"
        + "examples/orderbook_signal_001.json,\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source_lane"):
        load_manual_observations(csv_path)

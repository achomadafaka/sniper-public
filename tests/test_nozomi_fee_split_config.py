"""Fee allocation is an explicit, complete activation; transport policy stays unchanged."""
import pytest

from sniper.settings import memecoin_submission as config


def split_env():
    return {
        "MEMECOIN_NOZOMI_FEE_SPLIT_AB": "1",
        "MEMECOIN_SLOT_LANE_NOZOMI_AB": "1",
        "MEMECOIN_NOZOMI_ALL_B": "1",
        "MEMECOIN_NOZOMI_ALL_LANES": "1",
        "MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED": "1",
        "MEMECOIN_NOZOMI_API_KEY": "offline-test-key",
    }


@pytest.mark.parametrize("missing", [
    "MEMECOIN_SLOT_LANE_NOZOMI_AB", "MEMECOIN_NOZOMI_ALL_B",
    "MEMECOIN_NOZOMI_ALL_LANES", "MEMECOIN_SOLANA_ENTRY_BIDS_ENABLED",
    "MEMECOIN_NOZOMI_API_KEY",
])
def test_partial_split_activation_refuses_startup(missing):
    env = split_env()
    del env[missing]
    with pytest.raises(config.SubmissionConfigError, match="FEE_SPLIT"):
        config.validate_submission_config(env=env)


@pytest.mark.parametrize("value", ["2", "enabled", "maybe"])
def test_unknown_split_flag_refuses_startup(value):
    with pytest.raises(config.SubmissionConfigError, match="FEE_SPLIT"):
        config.validate_submission_config(env={**split_env(), "MEMECOIN_NOZOMI_FEE_SPLIT_AB": value})


def test_complete_activation_and_disabled_defaults_validate():
    config.validate_submission_config(env=split_env())
    config.validate_submission_config(env={"MEMECOIN_NOZOMI_FEE_SPLIT_AB": "0"})


def test_fee_split_is_in_atomic_provider_stage_scope():
    from scripts.research.shyft_migration_preflight import PROVIDER_KEYS
    assert "MEMECOIN_NOZOMI_FEE_SPLIT_AB" in PROVIDER_KEYS

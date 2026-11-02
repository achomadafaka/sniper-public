import os
import subprocess
import sys

import pytest

from sniper.settings.memecoin_opportunity import load_memecoin_opportunity_settings
from sniper.research.memecoin_opportunity.market_inputs import (
    NativeUsdPriceFeed,
    load_quote_market_inputs,
)
from sniper.research.memecoin_opportunity.types import VenueId


def test_opportunity_capture_is_default_off_and_uses_isolated_database():
    settings = load_memecoin_opportunity_settings({})
    assert settings.enabled is False
    assert settings.db_path.endswith("data/memecoin_opportunity.db")
    assert settings.clip_usd_micros == (5_000_000, 10_000_000, 25_000_000, 50_000_000)
    assert settings.primary_clip_usd_micros == 25_000_000
    assert settings.insight_db_path.endswith("data/memecoin_opportunity_insights.db")
    assert settings.hot_retention_s == 360
    assert settings.capture_window_s == 120
    assert settings.candidate_capture_window_s == 300
    assert settings.hot_emergency_retention_s == 300
    assert settings.insight_retention_s == 7 * 86_400
    assert settings.cleanup_interval_s == 30
    assert settings.hot_soft_cap_bytes == 128 * 1024 * 1024
    assert settings.hot_hard_cap_bytes == 256 * 1024 * 1024
    assert settings.rpc_concurrency == 1
    assert settings.rpc_initial_rps == 0.25
    assert settings.rpc_min_rps == 0.25
    assert settings.rpc_max_rps == 0.25
    assert settings.robinhood_rpc_rps == 1.0
    assert settings.solana_recovery_rpc_url == "https://api.mainnet-beta.solana.com"
    assert settings.solana_recovery_rps == 0.25
    assert settings.solana_indexed_recovery is False
    assert settings.recovery_max_pages == 30
    assert settings.solana_orphan_after_s == 120.0
    assert settings.robinhood_warm_start_blocks == 50_000
    assert settings.robinhood_poll_chunk_blocks == 5_000
    assert settings.quote_max_queue_age_ms == 2_000
    assert settings.quote_max_pair_age_ms == 3_500
    assert settings.quote_max_completion_age_ms == 1_000
    assert settings.quote_cache_max == 256
    assert settings.quote_completion_max == 256


def test_enabled_runtime_requires_at_least_one_read_only_source_url():
    with pytest.raises(ValueError, match="source URL"):
        load_memecoin_opportunity_settings({"MEMECOIN_OPPORTUNITY_CAPTURE": "1"})

    settings = load_memecoin_opportunity_settings({
        "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
        "MEMECOIN_OPPORTUNITY_SOLANA_RPC_URL": "https://solana.invalid",
    })
    assert settings.enabled is True
    assert settings.solana_rpc_url == "https://solana.invalid"

    with pytest.raises(ValueError, match="source URL"):
        load_memecoin_opportunity_settings({
            "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
            "MEMECOIN_OPPORTUNITY_BAGS_SOLANA_WS_URL": "wss://bags.invalid",
        })

    shared_read_settings = load_memecoin_opportunity_settings({
        "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
        "MEMECOIN_SOLANA_READ_RPC_URL": "https://shared-read.invalid",
    })
    assert shared_read_settings.solana_rpc_url == "https://shared-read.invalid"
    assert shared_read_settings.solana_recovery_rpc_url == "https://api.mainnet-beta.solana.com"

    explicit_recovery = load_memecoin_opportunity_settings({
        "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
        "MEMECOIN_SOLANA_READ_RPC_URL": "https://shared-read.invalid",
        "MEMECOIN_OPPORTUNITY_SOLANA_RECOVERY_RPC_URL": "https://recovery.invalid",
    })
    assert explicit_recovery.solana_recovery_rpc_url == "https://recovery.invalid"

    indexed_recovery = load_memecoin_opportunity_settings({
        "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
        "MEMECOIN_SOLANA_READ_RPC_URL": "https://indexed.invalid",
        "MEMECOIN_OPPORTUNITY_SOLANA_INDEXED_RECOVERY": "1",
    })
    assert indexed_recovery.solana_indexed_recovery is True
    assert indexed_recovery.solana_recovery_rps == 1.0

    explicit_indexed_rate = load_memecoin_opportunity_settings({
        "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
        "MEMECOIN_SOLANA_READ_RPC_URL": "https://indexed.invalid",
        "MEMECOIN_OPPORTUNITY_SOLANA_INDEXED_RECOVERY": "1",
        "MEMECOIN_OPPORTUNITY_SOLANA_RECOVERY_RPS": "0.5",
    })
    assert explicit_indexed_rate.solana_recovery_rps == 0.5


def test_emergency_retention_cannot_cut_off_candidate_evidence():
    with pytest.raises(ValueError, match="emergency retention must cover candidate capture"):
        load_memecoin_opportunity_settings({
            "MEMECOIN_OPPORTUNITY_HOT_EMERGENCY_RETENTION_S": "299",
        })

    with pytest.raises(ValueError, match="Solana WebSocket requires"):
        load_memecoin_opportunity_settings({
            "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
            "MEMECOIN_OPPORTUNITY_SOLANA_WS_URL": "wss://solana.invalid",
        })


def test_pinned_worker_loads_enablement_from_shared_state_root(tmp_path):
    (tmp_path / ".env").write_text(
        "MEMECOIN_OPPORTUNITY_CAPTURE=1\n"
        "MEMECOIN_OPPORTUNITY_SOLANA_RPC_URL=https://solana.invalid\n"
    )
    env = dict(os.environ)
    env.pop("MEMECOIN_OPPORTUNITY_CAPTURE", None)
    env.pop("MEMECOIN_OPPORTUNITY_SOLANA_RPC_URL", None)
    env["SNIPER_STATE_ROOT"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "scripts/memecoin_opportunity_worker.py", "--enabled-check"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("key,value", [
    ("MEMECOIN_OPPORTUNITY_QUEUE_MAX", "0"),
    ("MEMECOIN_OPPORTUNITY_STALE_AFTER_S", "nan"),
    ("MEMECOIN_OPPORTUNITY_RECOVERY_MAX_PAGES", "-1"),
    ("MEMECOIN_OPPORTUNITY_4663_POLL_S", "0"),
    ("MEMECOIN_OPPORTUNITY_4663_WARM_START_BLOCKS", "-1"),
    ("MEMECOIN_OPPORTUNITY_4663_POLL_CHUNK_BLOCKS", "0"),
    ("MEMECOIN_OPPORTUNITY_HOT_RETENTION_S", "0"),
    ("MEMECOIN_OPPORTUNITY_CAPTURE_WINDOW_S", "119"),
    ("MEMECOIN_OPPORTUNITY_CANDIDATE_CAPTURE_WINDOW_S", "299"),
    ("MEMECOIN_OPPORTUNITY_RPC_CONCURRENCY", "0"),
    ("MEMECOIN_OPPORTUNITY_SOLANA_RECOVERY_RPS", "0"),
    ("MEMECOIN_OPPORTUNITY_4663_RPC_RPS", "0"),
    ("MEMECOIN_OPPORTUNITY_SOLANA_ORPHAN_AFTER_S", "nan"),
    ("MEMECOIN_OPPORTUNITY_QUOTE_MAX_QUEUE_AGE_MS", "0"),
    ("MEMECOIN_OPPORTUNITY_QUOTE_MAX_PAIR_AGE_MS", "0"),
    ("MEMECOIN_OPPORTUNITY_QUOTE_MAX_COMPLETION_AGE_MS", "0"),
    ("MEMECOIN_OPPORTUNITY_QUOTE_CACHE_MAX", "0"),
    ("MEMECOIN_OPPORTUNITY_QUOTE_COMPLETION_MAX", "0"),
])
def test_invalid_runtime_limits_fail_closed(key, value):
    with pytest.raises(ValueError):
        load_memecoin_opportunity_settings({key: value})


def test_invalid_retention_cap_order_fails_closed():
    with pytest.raises(ValueError, match="soft cap"):
        load_memecoin_opportunity_settings({
            "MEMECOIN_OPPORTUNITY_HOT_SOFT_CAP_MB": "800",
            "MEMECOIN_OPPORTUNITY_HOT_HARD_CAP_MB": "750",
        })

    with pytest.raises(ValueError, match="candidate capture"):
        load_memecoin_opportunity_settings({
            "MEMECOIN_OPPORTUNITY_HOT_RETENTION_S": "329",
        })


def test_invalid_opportunity_env_cannot_break_shared_funded_settings_import():
    env = dict(os.environ)
    env["MEMECOIN_OPPORTUNITY_QUEUE_MAX"] = "invalid"
    result = subprocess.run(
        [sys.executable, "-c", "import sniper.settings.memecoin"],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_quote_market_inputs_require_timestamp_and_explicit_network_cost():
    ready, blocked = load_quote_market_inputs(
        {
            "MEMECOIN_OPPORTUNITY_SOL_USD_MICROS": "150000000",
            "MEMECOIN_OPPORTUNITY_SOL_USD_AS_OF": "2026-07-26T12:00:00Z",
        },
        now_iso="2026-07-26T12:00:01Z",
    )
    assert VenueId.PUMPFUN_SOLANA not in ready
    assert blocked[VenueId.PUMPFUN_SOLANA] == "MISSING_TIMESTAMPED_MARKET_INPUT"


def test_pump_market_input_defaults_to_current_bonding_curve_fee():
    ready, blocked = load_quote_market_inputs(
        {
            "MEMECOIN_OPPORTUNITY_SOL_USD_MICROS": "150000000",
            "MEMECOIN_OPPORTUNITY_SOL_USD_AS_OF": "2026-07-26T12:00:00Z",
            "MEMECOIN_OPPORTUNITY_SOL_NETWORK_COST_LAMPORTS": "5000",
        },
        now_iso="2026-07-26T12:00:01Z",
    )

    assert blocked.get(VenueId.PUMPFUN_SOLANA) is None
    assert ready[VenueId.PUMPFUN_SOLANA].fee_bps == 125


def test_quote_market_inputs_reject_stale_price():
    ready, blocked = load_quote_market_inputs(
        {
            "MEMECOIN_OPPORTUNITY_SOL_USD_MICROS": "150000000",
            "MEMECOIN_OPPORTUNITY_SOL_USD_AS_OF": "2026-07-26T11:00:00Z",
            "MEMECOIN_OPPORTUNITY_SOL_NETWORK_COST_LAMPORTS": "5000",
            "MEMECOIN_OPPORTUNITY_PRICE_MAX_AGE_S": "60",
        },
        now_iso="2026-07-26T12:00:01Z",
    )
    assert VenueId.PUMPFUN_SOLANA not in ready
    assert blocked[VenueId.PUMPFUN_SOLANA] == "STALE_NATIVE_USD_PRICE"


def test_live_native_price_feed_replaces_expired_env_prices_without_losing_costs():
    feed = NativeUsdPriceFeed(
        fetch_json=lambda: {
            "solana": {"usd": 151.25},
            "ethereum": {"usd": 3_625.50},
        },
        refresh_every_s=30,
    )

    ready, blocked = feed.load(
        {
            "MEMECOIN_OPPORTUNITY_SOL_USD_MICROS": "1",
            "MEMECOIN_OPPORTUNITY_SOL_USD_AS_OF": "2026-07-26T11:00:00Z",
            "MEMECOIN_OPPORTUNITY_SOL_NETWORK_COST_LAMPORTS": "5000",
            "MEMECOIN_OPPORTUNITY_4663_NATIVE_USD_MICROS": "1",
            "MEMECOIN_OPPORTUNITY_4663_NATIVE_USD_AS_OF": "2026-07-26T11:00:00Z",
            "MEMECOIN_OPPORTUNITY_4663_NETWORK_COST_WEI": "30000000000000",
        },
        now_iso="2026-07-26T12:00:00Z",
        monotonic_s=100.0,
    )

    assert blocked == {}
    assert ready[VenueId.PUMPFUN_SOLANA].usd_price_micros == 151_250_000
    # both bags identities are HISTORICAL_ONLY, so neither gets a live price input
    assert VenueId.BAGS_SOLANA not in ready
    assert VenueId.BAGS_SOLANA not in blocked
    assert VenueId.BAGS_ROBINHOOD_4663 not in ready
    assert VenueId.BAGS_ROBINHOOD_4663 not in blocked
    assert ready[VenueId.PONS_V1_ROBINHOOD_4663].usd_price_micros == 3_625_500_000
    assert ready[VenueId.PUMPFUN_SOLANA].network_cost_quote_atomic == 5000
    assert ready[VenueId.PONS_V1_ROBINHOOD_4663].network_cost_quote_atomic == 30000000000000
    assert ready[VenueId.PUMPFUN_SOLANA].price_as_of == "2026-07-26T12:00:00Z"


def test_retired_bags_coverage_setting_has_no_runtime_authority():
    settings = load_memecoin_opportunity_settings({
        "MEMECOIN_OPPORTUNITY_BAGS_COVERAGE_INTERVAL_S": "invalid",
    })

    assert not hasattr(settings, "bags_coverage_interval_s")


# --- paper adapter ------------------------------------------------------------

def _base_env(**overrides):
    env = {
        "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
        "MEMECOIN_OPPORTUNITY_SOLANA_RPC_URL": "https://rpc.example",
    }
    env.update(overrides)
    return env


def test_paper_adapter_is_default_off():
    settings = load_memecoin_opportunity_settings(env=_base_env())
    assert settings.paper_adapter_enabled is False
    assert settings.paper_adapter_expiry_s == 30.0
    assert settings.paper_adapter_consumer_max_age_s == 120.0
    assert settings.paper_adapter_consumer_heartbeat_path


def test_paper_adapter_opts_in_explicitly():
    settings = load_memecoin_opportunity_settings(
        env=_base_env(MEMECOIN_OPPORTUNITY_PAPER_ADAPTER="1")
    )
    assert settings.paper_adapter_enabled is True


def test_adaptive_v3_is_default_off():
    settings = load_memecoin_opportunity_settings(env=_base_env())

    assert settings.adaptive_v3_enabled is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_adaptive_v3_accepts_explicit_true_values(value):
    settings = load_memecoin_opportunity_settings(
        env=_base_env(
            MEMECOIN_OPPORTUNITY_PAPER_ADAPTER="1",
            MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED=value,
        )
    )

    assert settings.adaptive_v3_enabled is True


def test_adaptive_v3_requires_the_paper_adapter():
    with pytest.raises(ValueError, match="adaptive v3 requires the paper adapter"):
        load_memecoin_opportunity_settings(
            env=_base_env(MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED="1")
        )


def test_paper_adapter_expiry_must_be_positive_and_finite():
    with pytest.raises(ValueError, match="PAPER_ADAPTER_EXPIRY_S"):
        load_memecoin_opportunity_settings(
            env=_base_env(MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_EXPIRY_S="0")
        )
    with pytest.raises(ValueError, match="PAPER_ADAPTER_EXPIRY_S"):
        load_memecoin_opportunity_settings(
            env=_base_env(MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_EXPIRY_S="nan")
        )


def test_paper_adapter_consumer_max_age_must_be_positive():
    with pytest.raises(ValueError, match="CONSUMER_MAX_AGE_S"):
        load_memecoin_opportunity_settings(
            env=_base_env(MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_CONSUMER_MAX_AGE_S="-1")
        )


def test_enabled_paper_adapter_requires_a_consumer_heartbeat_path():
    with pytest.raises(ValueError, match="consumer heartbeat"):
        load_memecoin_opportunity_settings(
            env=_base_env(
                MEMECOIN_OPPORTUNITY_PAPER_ADAPTER="1",
                MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_CONSUMER_HEARTBEAT="  ",
            )
        )

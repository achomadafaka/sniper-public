"""Task 2 — inert, default-OFF V6 live-paper settings on sniper.settings.memecoin.

Defaults are asserted against the imported module attributes; validation is asserted against the
pure loader helper so no module reload (with its heavy import-time side effects) is required.
"""
import pytest

from sniper.settings import memecoin as settings


def test_v6_paper_defaults():
    assert settings.MEMECOIN_V6_PAPER_ENABLED is False
    assert settings.MEMECOIN_V6_PAPER_NOTIONAL_USD == 25.0
    assert settings.MEMECOIN_V6_PAPER_MAX_OPEN == 4
    assert settings.MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S == 10.0
    assert settings.MEMECOIN_V6_PAPER_SWEEP_S == 5.0
    assert settings.MEMECOIN_V6_EVENT_COALESCE_MS == 50
    assert settings.MEMECOIN_V6_HEARTBEAT_S == 1.0
    assert settings.MEMECOIN_V6_EVM_FALLBACK_POLL_MS == 500
    assert settings.MEMECOIN_V6_SOLANA_BINDING_REFRESH_S == 0.5
    assert settings.MEMECOIN_V6_QUOTE_REFRESH_S == 5.0
    assert settings.MEMECOIN_V6_EVENT_CATCHUP_MAX == 400
    assert settings.MEMECOIN_V6_PAPER_NETWORKS == ("solana", "4663")
    assert settings.MEMECOIN_V6_PAPER_ADMISSION_NETWORKS == ("4663",)
    # DB override is optional; persistence resolves the default under the mutable state root.
    assert settings.MEMECOIN_V6_PAPER_DB is None


def test_enabled_flag_parses_truthy():
    assert settings._load_v6_paper_settings({"MEMECOIN_V6_PAPER_ENABLED": "1"})["enabled"] is True
    assert settings._load_v6_paper_settings({"MEMECOIN_V6_PAPER_ENABLED": "yes"})["enabled"] is True
    assert settings._load_v6_paper_settings({})["enabled"] is False


def test_db_override_passthrough_and_default_none():
    assert settings._load_v6_paper_settings({"MEMECOIN_V6_PAPER_DB": "/x/y.sqlite"})["db"] == "/x/y.sqlite"
    assert settings._load_v6_paper_settings({})["db"] is None
    assert settings._load_v6_paper_settings({"MEMECOIN_V6_PAPER_DB": "   "})["db"] is None


@pytest.mark.parametrize("var", [
    "MEMECOIN_V6_PAPER_NOTIONAL_USD",
    "MEMECOIN_V6_PAPER_MAX_OPEN",
    "MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S",
    "MEMECOIN_V6_PAPER_SWEEP_S",
])
@pytest.mark.parametrize("bad", ["0", "-1"])
def test_non_positive_numeric_raises(var, bad):
    with pytest.raises(ValueError):
        settings._load_v6_paper_settings({var: bad})


def test_non_finite_numeric_raises():
    with pytest.raises(ValueError):
        settings._load_v6_paper_settings({"MEMECOIN_V6_PAPER_NOTIONAL_USD": "nan"})
    with pytest.raises(ValueError):
        settings._load_v6_paper_settings({"MEMECOIN_V6_PAPER_QUOTE_MAX_AGE_S": "inf"})


def test_unsupported_network_raises():
    with pytest.raises(ValueError):
        settings._load_v6_paper_settings({"MEMECOIN_V6_PAPER_NETWORKS": "solana,ethereum"})


def test_empty_networks_raises():
    with pytest.raises(ValueError):
        settings._load_v6_paper_settings({"MEMECOIN_V6_PAPER_NETWORKS": ""})


def test_supported_subset_ok_and_order_preserved():
    assert settings._load_v6_paper_settings(
        {"MEMECOIN_V6_PAPER_NETWORKS": "4663"})["networks"] == ("4663",)
    assert settings._load_v6_paper_settings(
        {"MEMECOIN_V6_PAPER_NETWORKS": "4663,solana"})["networks"] == ("4663", "solana")


def test_admission_networks_must_be_an_observed_subset():
    values = settings._load_v6_paper_settings({
        "MEMECOIN_V6_PAPER_NETWORKS": "solana,4663",
        "MEMECOIN_V6_PAPER_ADMISSION_NETWORKS": "4663",
    })
    assert values["admission_networks"] == ("4663",)
    with pytest.raises(ValueError, match="ADMISSION_NETWORKS"):
        settings._load_v6_paper_settings({
            "MEMECOIN_V6_PAPER_NETWORKS": "4663",
            "MEMECOIN_V6_PAPER_ADMISSION_NETWORKS": "solana",
        })

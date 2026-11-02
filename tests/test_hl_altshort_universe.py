import pytest

from sniper.hyperlive.altshort_universe import (
    ALT_SHORT_EXCLUDED,
    coin_comparison_key,
    is_alt_short,
    lookup_coin,
    normalize_coin,
    provider_coin,
    provider_coin_index,
    strategy_bucket,
)


def test_strategy_excludes_only_btc_eth_sol():
    assert ALT_SHORT_EXCLUDED == frozenset({"BTC", "ETH", "SOL"})
    assert strategy_bucket(" BTC ") == "core"
    assert strategy_bucket("bnb") == "alt"
    assert strategy_bucket("DOGE") == "alt"


def test_normalization_and_missing_values_fail_closed():
    assert normalize_coin(" cashcat ") == "CASHCAT"
    assert normalize_coin("") is None
    assert normalize_coin(None) is None
    assert strategy_bucket(None) == "unknown"
    assert is_alt_short(" cashcat ", " SHORT ") is True
    assert is_alt_short("SOL", "short") is False
    assert is_alt_short(None, "short") is False
    assert is_alt_short("cashcat", None) is False


def test_provider_identity_is_preserved_while_comparisons_are_case_insensitive():
    assert provider_coin(" kPEPE ") == "kPEPE"
    assert provider_coin("KAS") == "KAS"
    assert coin_comparison_key("kPEPE") == "KPEPE"
    assert normalize_coin("kPEPE") == "KPEPE"


def test_provider_index_rejects_case_collisions_and_lookup_preserves_source_key():
    assert provider_coin_index(["kPEPE", "KAS"]) == {
        "KPEPE": "kPEPE",
        "KAS": "KAS",
    }
    assert lookup_coin({"kBONK": 2.0, "KAS": 1.5}, "KBONK") == ("kBONK", 2.0)
    with pytest.raises(ValueError, match="ambiguous provider coin identity: KPEPE"):
        provider_coin_index(["kPEPE", "KPEPE"])

"""Alt-short filter: the copy edge lives in non-major shorts only."""
from sniper.hyperlive.copy_filters import EntryPolicy, is_alt_short, should_open
from sniper.hyperlive.real_exec import eligibility


def test_is_alt_short():
    assert is_alt_short("HEMI", "short") is True
    assert is_alt_short("cashcat", "short") is True          # case-insensitive
    assert is_alt_short("BTC", "short") is False             # major
    assert is_alt_short("ETH", "short") is False
    assert is_alt_short("HEMI", "long") is False             # wrong side
    assert is_alt_short(None, "short") is False              # fail closed on missing
    assert is_alt_short("HEMI", None) is False


def test_should_open_alt_short_only():
    pol = EntryPolicy(alt_short_only=True)
    assert pol.active is True
    assert should_open(coin="HEMI", side="short", policy=pol)[0] is True
    assert should_open(coin="BTC", side="short", policy=pol)[0] is False   # major skipped
    assert should_open(coin="HEMI", side="long", policy=pol)[0] is False   # long skipped
    assert should_open(coin=None, side=None, policy=pol)[0] is False       # fail closed
    # default policy filters nothing (backward compatible)
    assert should_open(coin="BTC", side="long", policy=EntryPolicy())[0] is True


def test_real_eligibility_alt_short_gate():
    row = {"target": "0xabc", "coin": "HEMI", "side": "long", "copy_tier": "proven",
           "is_cold_start": 0, "detect_ts_ms": 100}
    # alt_short_only off -> passes the coin/side gate (long allowed)
    assert eligibility(row, allow_targets=["0xabc"], epoch_ms=0, alt_short_only=False)[0] is True
    # alt_short_only on -> a long is rejected as not_alt_short
    ok, why = eligibility(row, allow_targets=["0xabc"], epoch_ms=0, alt_short_only=True)
    assert ok is False and why == "not_alt_short"
    # a non-major short passes
    row_s = {**row, "side": "short"}
    assert eligibility(row_s, allow_targets=["0xabc"], epoch_ms=0, alt_short_only=True)[0] is True
    # allowlist still enforced first
    assert eligibility(row_s, allow_targets=["0xother"], epoch_ms=0, alt_short_only=True) == (
        False, "target_not_allowed")

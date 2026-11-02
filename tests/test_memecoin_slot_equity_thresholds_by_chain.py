"""Per-chain equity thresholds for the slot tiers (operator 2026-10-08).

Chain 1 runs a $75 minimum clip because mainnet gas makes a $50 clip cost 10-17% a round trip.
The global tiers open slot 2 at $100 of equity, so a chain-1 book of $110 would split into two
$55 clips and fall back under the gas cost cap. The operator's order: "unlock more slots as the
equity grows" -- a slot opens only when the equity can pay a $75 clip in every open slot.
`MEMECOIN_TWO_SLOT_EQUITY_USD_<chain>` / `MEMECOIN_THREE_SLOT_EQUITY_USD_<chain>` override the
global thresholds for one chain; every other chain keeps the global values.
"""
import pytest

from sniper.services.memecoin_risk import real_trade_size


@pytest.fixture
def cfg(monkeypatch):
    import sniper.settings.memecoin as _cfg
    monkeypatch.setattr(_cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {"1": 3, "4663": 3})
    monkeypatch.setattr(_cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN", {"1": True, "4663": True})
    monkeypatch.setattr(_cfg, "MEMECOIN_TWO_SLOT_EQUITY_USD", 100.0)
    monkeypatch.setattr(_cfg, "MEMECOIN_THREE_SLOT_EQUITY_USD", 300.0)
    monkeypatch.setattr(_cfg, "MEMECOIN_EVM_TIER_CLIP_USD_BY_CHAIN", {"1": (75.0, 175.0, 175.0)})
    monkeypatch.setenv("MEMECOIN_TWO_SLOT_EQUITY_USD_1", "150")
    monkeypatch.setenv("MEMECOIN_THREE_SLOT_EQUITY_USD_1", "225")
    return _cfg


def test_chain_override_moves_the_slot_boundaries(cfg):
    slots = {e: cfg.max_concurrent_for("1", equity=e) for e in (97, 110, 149.99, 150, 224.99, 225, 900)}
    assert slots == {97: 1, 110: 1, 149.99: 1, 150: 2, 224.99: 2, 225: 3, 900: 3}


def test_other_chains_keep_the_global_thresholds(cfg):
    assert cfg.max_concurrent_for("4663", equity=110) == 2
    assert cfg.max_concurrent_for("4663", equity=299) == 2
    assert cfg.max_concurrent_for("4663", equity=300) == 3


@pytest.mark.parametrize("raw", ["", "abc", "nan", "-5"])
def test_malformed_override_keeps_the_global_value(cfg, monkeypatch, raw):
    monkeypatch.setenv("MEMECOIN_TWO_SLOT_EQUITY_USD_1", raw)
    monkeypatch.setenv("MEMECOIN_THREE_SLOT_EQUITY_USD_1", raw)
    assert cfg.max_concurrent_for("1", equity=110) == 2
    assert cfg.max_concurrent_for("1", equity=300) == 3


def test_three_below_two_never_opens_three_before_two(cfg, monkeypatch):
    monkeypatch.setenv("MEMECOIN_THREE_SLOT_EQUITY_USD_1", "120")
    assert cfg.max_concurrent_for("1", equity=130) == 1
    assert cfg.max_concurrent_for("1", equity=150) == 3


def test_chain_one_clip_never_drops_under_75_once_equity_covers_it(cfg):
    """The operator's invariant: from $75 of equity up, every clip on chain 1 is at least $75."""
    for cents in range(7500, 100001, 25):
        equity = cents / 100.0
        slots = cfg.max_concurrent_for("1", equity=equity)
        clip = real_trade_size(usdc_balance=equity, open_notional_usd=0.0, max_concurrent=slots,
                               max_clip_usd=cfg.evm_max_clip_usd_for("1", slots=slots),
                               mode="bankroll")
        clip = min(clip, cfg.evm_max_clip_usd_for("1", slots=slots))
        assert clip >= 75.0, (equity, slots, clip)

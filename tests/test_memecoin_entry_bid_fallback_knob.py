"""MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS (Alpha 2026-10-15): the slot lane's normal bid on a
cold or stale pool (every first buy of a new coin today) was the 1,520,000-lamport constant
FALLBACK_PRIORITY_LAMPORTS. The knob makes it total lamports, read at call time; non-int or <= 0
is the 1,520,000 default. It stays under MAX_TOTAL_LAMPORTS minus the tip, under a crowded override,
and the fresh pool p95 path is unchanged."""
import pytest

from sniper.execution import pump_curve_buy as curve
from sniper.execution.solana_entry_bids import EntryBidCache
from sniper.settings import memecoin as cfg
from test_memecoin_crowded_fee import _signed_priority


def _cold():
    def none(*a, **k):
        raise AssertionError("no provider read on the signing path")
    return EntryBidCache(none, none)                     # no sample: every pool reads cold


def _signed_normal_bid(tip=0, limit=400_000):
    """Through the lane's own selection (select_priority_fee over EntryBidCache.select) and the real
    slot_execute_fn signing seam: the priority fee in the bytes that were signed."""
    builder = type("B", (), {"priority_fee_fn": staticmethod(_cold().select), "priority_fee_microlamports": None})()
    price, bid = curve.select_priority_fee(builder, "PoolCold111111111111111111111111111111111111", limit,
                                           ("tip", tip) if tip else None)
    signed, signed_bid = _signed_priority({}, tip=tip, price=price, limit=limit)
    return signed, bid, signed_bid


def test_the_knob_reaches_the_signed_priority(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", 5_000_000, raising=False)
    signed, bid, _ = _signed_normal_bid()
    assert bid["priority_source"] == "fallback_cold" and bid["priority_fee_lamports"] == 5_000_000
    assert signed == 5_000_000


@pytest.mark.parametrize("value", [None, 0, -1, "5000000", 2.5, True])
def test_an_absent_or_malformed_knob_signs_todays_1_52m(monkeypatch, value):
    if value is None:
        monkeypatch.delattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", raising=False)
    else:
        monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", value, raising=False)
    signed, bid, _ = _signed_normal_bid()
    assert bid["priority_fee_lamports"] == 1_520_000 and signed == 1_520_000


def test_the_knob_stays_under_the_total_cap_with_the_tip(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", 9_000_000, raising=False)
    signed, bid, _ = _signed_normal_bid(tip=1_000_000)
    assert signed == 5_520_000 and bid["cap_saturated"] is True             # 6.52M - the 1M tip


def test_a_crowded_send_still_bids_its_target_over_the_knob(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", 5_000_000, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", 16_000_000)
    builder = type("B", (), {"priority_fee_fn": staticmethod(_cold().select), "priority_fee_microlamports": None})()
    price, bid = curve.select_priority_fee(builder, "PoolCold111111111111111111111111111111111111", 400_000,
                                           None, override_lamports=10_000_000)
    signed, _ = _signed_priority({"crowded_priority_lamports": 10_000_000}, tip=0, price=price, limit=400_000)
    assert bid["crowded"] == "raised" and signed == 10_000_000


def test_the_settings_default_is_todays_constant():
    import importlib
    import os
    fresh = importlib.reload(cfg)
    assert fresh.MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS == 1_520_000 \
        or "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS" in os.environ


# --- the warm-pool floor tracks the cold fallback (Alpha 2026-10-16) ---------------------------------
# A warm pool's floor was PRIORITY_FLOOR_MICRO_LAMPORTS (3.8M uL/CU = 1.52M lamports at 400k CU) while a
# cold pool bid the 5M knob: a quiet pool's low p95 signed under the cold fallback (Gamma saw 1.52M).

from solders.pubkey import Pubkey  # noqa: E402

WARM = str(Pubkey.from_bytes(bytes([7]) * 32))


def _warm(p95):
    c = EntryBidCache(lambda pool: [{"slot": 100, "prioritizationFee": p95}], None,      # no tip read
                      clock=lambda: 0.0, wall_clock=lambda: 1_800_000_000)
    c.register(WARM)
    c._refresh_once()
    return c


def _signed_warm_bid(p95, limit=400_000):
    builder = type("B", (), {"priority_fee_fn": staticmethod(_warm(p95).select), "priority_fee_microlamports": None})()
    price, bid = curve.select_priority_fee(builder, WARM, limit, None)
    signed, _ = _signed_priority({}, tip=0, price=price, limit=limit)
    return signed, bid


def test_a_warm_pool_with_a_low_p95_bids_its_measured_fee_not_the_cold_fallback(monkeypatch):
    # operator 2026-10-23: the 10-16 floor at the fallback is removed; the 3.8M uL/CU constant floor stays
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", 5_000_000, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_WARM_FLOOR_AT_FALLBACK", False, raising=False)
    signed, bid = _signed_warm_bid(1_000_000)
    assert bid["priority_source"] == "account_filtered_p95" and bid["priority_sample_micro_lamports_per_cu"] == 1_000_000
    assert signed == 1_520_000                                                  # 3.8M uL/CU x 400k CU


def test_the_knob_restores_the_floor_at_the_cold_fallback(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", 5_000_000, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_WARM_FLOOR_AT_FALLBACK", True, raising=False)
    signed, _ = _signed_warm_bid(1_000_000)
    assert signed == 5_000_000


def test_a_warm_p95_above_the_fallback_floor_still_wins(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", 5_000_000, raising=False)
    signed, _ = _signed_warm_bid(15_000_000)                                  # 6.0M at 400k CU
    assert signed == 6_000_000


def test_without_the_knob_a_warm_pool_at_400k_cu_signs_todays_1_52m(monkeypatch):
    monkeypatch.delattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", raising=False)
    signed, _ = _signed_warm_bid(1_000_000)
    assert signed == 1_520_000


def test_the_3_8m_unit_floor_still_binds_when_the_fallback_over_cu_is_lower(monkeypatch):
    """Gamma LOW 2026-10-16: at CU 1.4M with the knob unset, 1.52M x 1e6 // 1.4M = 1,085,714 uL/CU, so
    the PRIORITY_FLOOR_MICRO_LAMPORTS term (3.8M) is the floor; dropping it would bid 1.52M total."""
    monkeypatch.delattr(cfg, "MEMECOIN_SOLANA_ENTRY_BID_FALLBACK_LAMPORTS", raising=False)
    bid = _warm(1_000_000).select(pool=WARM, cu_limit=1_400_000)
    assert bid["micro_lamports_per_cu"] == 3_800_000 and bid["priority_fee_lamports"] == 5_320_000

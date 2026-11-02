"""Phase 1 Task 6 — compute_state assembly + explicit non-actionable states (exit brain)."""
from datetime import datetime, timezone

from sniper.services.memecoin_exit_market_state import compute_state


def _iso(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()


def _good_inputs(base_ep):
    # 6 alternating buys/sells across blocks 1..6, one v2_sync for reserve
    swaps = []
    amts = [("100", "-50"), ("-100", "60"), ("120", "-50"), ("-90", "55"), ("110", "-48"), ("-80", "50")]
    for i, (a0, a1) in enumerate(amts, start=1):
        swaps.append({"kind": "v2_swap", "pool": "0xp", "block_no": i, "maker": "0xR",
                      "amount0": a0, "amount1": a1})
    swaps.append({"kind": "v2_sync", "pool": "0xp", "block_no": 6, "maker": None,
                  "amount0": "1000000", "amount1": "2000000"})
    block_times = {i: _iso(base_ep + i) for i in range(1, 7)}
    contract = {"chain_id": "4663", "mint": "COIN", "pool": "0xp", "pool_kind": "v2",
                "position_id": 7, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}
    return swaps, block_times, contract


def test_solana_is_source_untrusted():
    s = compute_state([], {}, {"chain_id": "solana", "position_id": 1}, cfg=None, now_ts=100)
    assert s.state.name == "SOURCE_UNTRUSTED" and not s.candidate_capable()


def test_missing_blocktime_is_data_insufficient():
    ev = compute_state([{"kind": "v3_swap", "block_no": 5, "amount0": "1", "amount1": "-1"}], {},
                       {"chain_id": "4663", "pool_kind": "v3", "position_id": 2, "quote_side": 0},
                       cfg=None, now_ts=100)
    # Spec §3.5: an incomplete required window is DATA_INSUFFICIENT — strictly, not "or WARMUP".
    assert ev.state.name == "DATA_INSUFFICIENT" and not ev.candidate_capable()


def test_good_v2_position_is_actionable_with_features():
    now = 1_700_000_100
    swaps, bt, contract = _good_inputs(1_700_000_000)
    s = compute_state(swaps, bt, contract, cfg=None, now_ts=now)
    assert s.candidate_capable()                      # data present, fresh -> actionable
    assert s.nominal_price is not None and s.executable_price is None   # never fabricated
    assert s.realized_vol is not None and s.realized_vol > 0
    assert s.buy_n == 3 and s.sell_n == 3
    assert s.reserve_quote == "1000000"               # V2 Sync quote reserve (depth)
    assert s.wash_filter == "disabled_router_ambiguity"
    assert "flow" in s.provenance and s.provenance["flow"].quality == "trusted"


def test_stale_data_is_flagged():
    now = 1_700_050_000                                # ~50k s after the ticks -> stale
    swaps, bt, contract = _good_inputs(1_700_000_000)
    s = compute_state(swaps, bt, contract, cfg=None, now_ts=now)
    assert s.state.name == "DATA_STALE" and not s.candidate_capable()


def test_compute_state_is_deterministic():
    now = 1_700_000_100
    swaps, bt, contract = _good_inputs(1_700_000_000)
    a = compute_state(swaps, bt, contract, cfg=None, now_ts=now)
    b = compute_state(swaps, bt, contract, cfg=None, now_ts=now)
    assert a == b

"""Phase 1 hardening regressions (2026-07-23 review): the two P0s + the fail-closed / semantics P1s.

These drive the PRODUCTION path — the real `decode_evm_log` tick decoder into `compute_state` where
it matters — with controlled inputs whose raw atomic ratios do NOT resemble the human-unit peak, so a
unit-domain mistake cannot hide behind a self-agreeing fixture (review §"tests are mostly
self-consistent fixtures").
"""
from datetime import datetime, timezone

import pytest

from sniper.analysis.exit_features import (
    arrival_rate_window, exec_price_path_per_block, profile_capability,
)
from sniper.services.memecoin_exit_market_state import compute_state
from sniper.services.memecoin_tick_stream import decode_evm_log

V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V2_SYNC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"


def _iso(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()


def _word(n):
    return f"{n & (2**256 - 1):064x}"


def _v2_swap_log(pool, block_no, a0_in, a1_in, a0_out, a1_out, log_index=0):
    """A real Uniswap-V2 Swap log (amount0In, amount1In, amount0Out, amount1Out), as eth_getLogs emits it."""
    data = "0x" + _word(a0_in) + _word(a1_in) + _word(a0_out) + _word(a1_out)
    return {"address": pool, "blockNumber": hex(block_no), "logIndex": hex(log_index),
            "transactionHash": f"0x{block_no:064x}", "topics": [V2_SWAP, "0x" + _word(0xABC)],
            "data": data}


def _v2_sync_log(pool, block_no, reserve0, reserve1, log_index=1):
    data = "0x" + _word(reserve0) + _word(reserve1)
    return {"address": pool, "blockNumber": hex(block_no), "logIndex": hex(log_index),
            "transactionHash": f"0x{block_no:064x}", "topics": [V2_SYNC], "data": data}


def _decode(logs, chain_id="4663"):
    return [decode_evm_log(chain_id, lg, observed_at="2026-07-23T00:00:00+00:00") for lg in logs]


# ---------------------------------------------------------------------------
# P0 #1 — candidate capability requires every required feature family trusted
# ---------------------------------------------------------------------------

def _v2_swaps_no_sync(base_ep):
    # six alternating buys/sells across blocks 1..6, produced by the PRODUCTION decoder, NO Sync
    logs = []
    for i in range(1, 7):
        if i % 2:  # buy: token0 (quote) into pool
            logs.append(_v2_swap_log("0xpool", i, a0_in=100 + i, a1_in=0, a0_out=0, a1_out=40 + i))
        else:      # sell: token0 out of pool
            logs.append(_v2_swap_log("0xpool", i, a0_in=0, a1_in=50 + i, a0_out=90 + i, a1_out=0))
    return _decode(logs), {i: _iso(base_ep + i) for i in range(1, 7)}


def test_v2_without_sync_depth_is_not_candidate_capable():
    base = 1_700_000_000
    swaps, bt = _v2_swaps_no_sync(base)
    c = {"chain_id": "4663", "mint": "COIN", "pool": "0xpool", "pool_kind": "v2",
         "position_id": 1, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    # depth is REQUIRED for a V2 profile and there is no Sync -> untrusted -> must not act.
    # WO-1 pull-lane fix: the label stays actionable; the depth-gated rules can't fire (per-family).
    assert not s.candidate_capable()
    assert s.candidate_capable(("depth",)) is False
    assert s.provenance["depth"].quality == "untrusted"
    assert "depth" in s.required_families


def test_unknown_chain_is_source_untrusted():
    base = 1_700_000_000
    swaps, bt = _v2_swaps_no_sync(base)
    c = {"chain_id": "bogus", "mint": "COIN", "pool": "0xpool", "pool_kind": "v2",
         "position_id": 1, "quote_side": 0, "entry_price": 1.8}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    assert s.state.name == "SOURCE_UNTRUSTED" and not s.candidate_capable()


def test_profile_capability_fails_closed_on_unknown_chain():
    cap = profile_capability("bogus", "v2")
    assert cap.forced_legacy and not cap.flow_trusted and not cap.required
    known = profile_capability("4663", "v2")
    assert known.required == frozenset({"price", "flow", "vol", "depth"})
    v3 = profile_capability("8453", "v3")
    assert v3.required == frozenset({"price", "flow", "vol"}) and not v3.depth_trusted


def test_v3_is_actionable_without_depth():
    """V3 forces depth unavailable, so depth is NOT required — flow/vol/structure alone can act."""
    base = 1_700_000_000
    # V3 rows carry signed amount0/amount1 directly (decoder emits them from the two data words)
    swaps = [{"kind": "v3_swap", "pool": "0xp", "block_no": i,
              "amount0": str((100 + i) if i % 2 else -(90 + i)),
              "amount1": str(-(40 + i) if i % 2 else (50 + i))} for i in range(1, 7)]
    bt = {i: _iso(base + i) for i in range(1, 7)}
    # 4663 V3 depth is now verified and required; 8453 preserves the depth-unavailable profile.
    c = {"chain_id": "8453", "mint": "C", "pool": "0xp", "pool_kind": "v3",
         "position_id": 2, "quote_side": 0, "entry_price": 1.0, "executable_peak": 2.0}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    assert s.state.name == "TRENDING" and s.candidate_capable()
    assert s.provenance["depth"].quality == "untrusted"   # unavailable, but not required
    assert "depth" not in s.required_families


# ---------------------------------------------------------------------------
# P0 #2 — print prices never compared with the human-unit whole-bag peak
# ---------------------------------------------------------------------------

def test_drawdown_from_exec_peak_stays_none_until_fast_lane():
    """Atomic-ratio print prices (~2.x) and a human-unit executable_peak (2.4) are different units;
    the engine must NOT synthesize a drawdown from them (spec §3.4). Both stay None."""
    base = 1_700_000_000
    logs = [_v2_swap_log("0xp", i, a0_in=(100 + i if i % 2 else 0), a1_in=(0 if i % 2 else 50 + i),
                         a0_out=(0 if i % 2 else 90 + i), a1_out=(40 + i if i % 2 else 0)) for i in range(1, 7)]
    logs.append(_v2_sync_log("0xp", 6, reserve0=1_000_000, reserve1=2_000_000))
    swaps = _decode(logs)
    bt = {i: _iso(base + i) for i in range(1, 7)}
    c = {"chain_id": "4663", "mint": "C", "pool": "0xp", "pool_kind": "v2",
         "position_id": 3, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    assert s.candidate_capable()
    assert s.drawdown_from_exec_peak is None
    assert s.drawdown_velocity is None            # would be dimensionally invalid; must be None
    assert s.executable_price is None
    assert s.nominal_peak is not None             # nominal (print) peak is honest and populated


def test_per_block_aggregation_collapses_same_block_prints():
    """Two prints in one block -> one size-weighted point, so equal timestamps can't degenerate Δt."""
    swaps = [
        {"kind": "v2_swap", "block_no": 1, "block_ts": 10, "amount0": "100", "amount1": "-50"},
        {"kind": "v2_swap", "block_no": 1, "block_ts": 10, "amount0": "300", "amount1": "-100"},  # same block
        {"kind": "v2_swap", "block_no": 2, "block_ts": 11, "amount0": "-80", "amount1": "40"},
    ]
    path = exec_price_path_per_block(swaps, quote_side=0)
    assert len(path) == 2                          # collapsed to one point per block
    ts0, price0, n0, blk0 = path[0]
    assert blk0 == 1
    assert n0 == 2 and ts0 == 10
    assert abs(price0 - (400 / 150)) < 1e-9        # base-weighted VWAP = (100+300)/(50+100)


# ---------------------------------------------------------------------------
# P1 — fixed-window arrival rate, not the elastic first-to-last span
# ---------------------------------------------------------------------------

def test_arrival_rate_is_fixed_window_not_elastic_span():
    # two swaps in the final second of a 300s window -> 2/300 per second, never 2/1
    swaps = [{"amount0": "100", "amount1": "-1", "block_ts": 299.5},
             {"amount0": "100", "amount1": "-1", "block_ts": 300.0}]
    rate = arrival_rate_window(swaps, quote_side=0, window_s=300.0, window_end_ts=300.0)
    assert abs(rate - (2 / 300.0)) < 1e-9


def test_arrival_accel_populated_on_actionable_state():
    base = 1_700_000_000
    logs = [_v2_swap_log("0xp", i, a0_in=(100 + i if i % 2 else 0), a1_in=(0 if i % 2 else 50 + i),
                         a0_out=(0 if i % 2 else 90 + i), a1_out=(40 + i if i % 2 else 0)) for i in range(1, 7)]
    logs.append(_v2_sync_log("0xp", 6, reserve0=1_000_000, reserve1=2_000_000))
    swaps = _decode(logs)
    bt = {i: _iso(base + i) for i in range(1, 7)}
    c = {"chain_id": "4663", "mint": "C", "pool": "0xp", "pool_kind": "v2",
         "position_id": 4, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    assert s.arrival_rate is not None and s.arrival_accel is not None


# ---------------------------------------------------------------------------
# P1 — fail closed on partial coverage and future/negative freshness
# ---------------------------------------------------------------------------

def test_partial_block_time_map_is_data_insufficient():
    base = 1_700_000_000
    swaps, bt = _v2_swaps_no_sync(base)
    del bt[6]                                       # one block loses its timestamp
    c = {"chain_id": "4663", "mint": "C", "pool": "0xpool", "pool_kind": "v2",
         "position_id": 5, "quote_side": 0, "entry_price": 1.8}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    assert s.state.name == "DATA_INSUFFICIENT" and not s.candidate_capable()


def test_future_event_time_fails_closed():
    base = 1_700_000_000
    swaps, bt = _v2_swaps_no_sync(base)
    # now_ts BEFORE the newest event -> negative freshness -> corrupt clock -> non-actionable
    s = compute_state(swaps, bt, {"chain_id": "4663", "pool": "0xpool", "pool_kind": "v2",
                                  "position_id": 6, "quote_side": 0}, cfg=None, now_ts=base - 100)
    assert s.state.name == "DATA_INSUFFICIENT" and not s.candidate_capable()


# ---------------------------------------------------------------------------
# P1 — deep immutability of the value object
# ---------------------------------------------------------------------------

def test_market_state_provenance_is_deeply_immutable():
    s = compute_state([], {}, {"chain_id": "solana", "position_id": 1}, cfg=None, now_ts=100)
    with pytest.raises(TypeError):
        s.provenance["price"] = "tampered"          # MappingProxyType rejects writes


# ---------------------------------------------------------------------------
# Round-2 review: per-family staleness, empty-required, depth_pull, serialization
# ---------------------------------------------------------------------------

def _v2_full(base, sync_block, sync_ep):
    """6 alternating swaps (blocks 1..6) + one Sync at `sync_block`; block_times lets each event's
    freshness be controlled independently so a fresh Sync can't hide stale swaps."""
    logs = [_v2_swap_log("0xp", i, a0_in=(100 + i if i % 2 else 0), a1_in=(0 if i % 2 else 50 + i),
                         a0_out=(0 if i % 2 else 90 + i), a1_out=(40 + i if i % 2 else 0)) for i in range(1, 7)]
    logs.append(_v2_sync_log("0xp", sync_block, reserve0=1_000_000, reserve1=2_000_000))
    bt = {i: _iso(base + i) for i in range(1, 7)}
    bt[sync_block] = _iso(sync_ep)
    return _decode(logs), bt


def test_stale_required_flow_not_candidate_capable_despite_fresh_sync():
    base = 1_700_000_000
    swaps, bt = _v2_full(base, sync_block=7, sync_ep=base + 1005)   # Sync fresh, swaps ~1000s old
    c = {"chain_id": "4663", "mint": "C", "pool": "0xp", "pool_kind": "v2",
         "position_id": 1, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 1006)   # > stale bound for the swaps
    # WO-1 pull-lane fix: a fresh Sync no longer launders stale swaps by collapsing the state; the
    # label stays actionable and the flow-gated rules can't fire (flow degraded, not trusted).
    assert not s.candidate_capable()
    assert s.candidate_capable(("flow",)) is False
    assert s.provenance["flow"].quality == "degraded"              # own freshness, not the Sync's


def test_empty_required_families_on_actionable_state_fails_closed():
    from sniper.analysis.exit_state_types import MarketState, StateLabel
    s = MarketState(chain_id="4663", mint="C", pool="p", position_id=1,
                    engine_version="v", feature_version="v", state=StateLabel.TRENDING)
    assert not s.candidate_capable()   # actionable label but nothing required -> malformed -> closed


def test_falling_reserve_without_a_burn_is_not_a_pull():
    """The disambiguation the flag exists for: reserve0 drops hard across two Syncs, but no Burn was
    emitted — that is ordinary buying, NOT an LP exit. Before Burn was decoded this had to report
    None (unknown); now it can honestly report False."""
    base = 1_700_000_000
    logs = [_v2_swap_log("0xp", i, a0_in=(100 + i if i % 2 else 0), a1_in=(0 if i % 2 else 50 + i),
                         a0_out=(0 if i % 2 else 90 + i), a1_out=(40 + i if i % 2 else 0)) for i in range(1, 7)]
    logs.append(_v2_sync_log("0xp", 3, reserve0=5_000_000, reserve1=1_000_000))
    logs.append(_v2_sync_log("0xp", 6, reserve0=1_000_000, reserve1=1_000_000, log_index=2))  # reserve0 fell
    swaps = _decode(logs)
    bt = {i: _iso(base + i) for i in range(1, 7)}
    c = {"chain_id": "4663", "mint": "C", "pool": "0xp", "pool_kind": "v2",
         "position_id": 2, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    assert s.reserve_delta is not None and int(s.reserve_delta) < 0   # the factual delta is kept
    assert s.depth_pull_flag is False                                 # a big drop, but nobody pulled LP


def test_to_dict_is_json_serializable():
    import json
    base = 1_700_000_000
    swaps, bt = _v2_full(base, sync_block=6, sync_ep=base + 6)
    c = {"chain_id": "4663", "mint": "C", "pool": "0xp", "pool_kind": "v2",
         "position_id": 3, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}
    s = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    d = s.to_dict()
    blob = json.dumps(d)                       # must not raise (asdict on the proxy would)
    assert '"state": "trending"' in blob
    assert d["provenance"]["price"]["price_kind"] == "block_vwap_exec_atomic"


def test_depth_pull_flag_is_real_now_that_burn_is_decoded():
    """The reason it was permanently None: a falling reserve is also an ordinary buy. A decoded
    v2_burn is unambiguous LP removal, so True/False/None now mean pulled / not-pulled / unknown."""
    base = 1_700_000_000
    swaps = [{"kind": "v2_swap", "block_no": i,
              "amount0": str((100 + i) if i % 2 else -(90 + i)),
              "amount1": str(-(40 + i) if i % 2 else (50 + i))} for i in range(1, 7)]
    swaps.append({"kind": "v2_sync", "block_no": 6, "amount0": "1000000", "amount1": "2000000"})
    bt = {i: _iso(base + i) for i in range(1, 7)}
    c = {"chain_id": "4663", "mint": "C", "pool": "0xp", "pool_kind": "v2",
         "position_id": 1, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}

    quiet = compute_state(swaps, bt, c, cfg=None, now_ts=base + 7)
    assert quiet.depth_pull_flag is False            # depth trusted, no burn -> known "no pull"

    pulled = compute_state(swaps + [{"kind": "v2_burn", "block_no": 6,
                                     "amount0": "-500000", "amount1": "-1000000"}],
                           bt, c, cfg=None, now_ts=base + 7)
    assert pulled.depth_pull_flag is True            # LP actually exited

    v3c = dict(c, pool_kind="v3")
    v3 = compute_state([{"kind": "v3_swap", "block_no": i,
                         "amount0": str((100 + i) if i % 2 else -(90 + i)),
                         "amount1": str(-(40 + i) if i % 2 else (50 + i))} for i in range(1, 7)],
                       bt, v3c, cfg=None, now_ts=base + 7)
    assert v3.depth_pull_flag is None                # V3 has no depth at all -> unknown, not False


def test_per_block_vwap_keys_on_block_identity_not_timestamp():
    """P1 (Codex 2026-07-24): block_times stores SECOND resolution and 4663 makes ~10 blocks/sec,
    so keying the 'per-block' VWAP on block_ts merged distinct blocks — locally 933,586 blocks into
    14,555 timestamps. Two different blocks sharing one timestamp must stay two points."""
    from sniper.analysis.exit_features import exec_price_path_per_block
    swaps = [
        {"kind": "v2_swap", "block_no": 100, "block_ts": 10, "amount0": "100", "amount1": "-50"},
        {"kind": "v2_swap", "block_no": 101, "block_ts": 10, "amount0": "300", "amount1": "-50"},
    ]
    path = exec_price_path_per_block(swaps, quote_side=0)
    assert len(path) == 2, "distinct blocks sharing a timestamp were merged"
    assert [n for _ts, _px, n, _b in path] == [1, 1]
    assert [b for _ts, _px, _n, b in path] == [100, 101]   # ordered by block identity
    # and prices stay distinct (2.0 vs 6.0), not blended into one 4.0 bucket
    assert sorted(round(px, 6) for _ts, px, _n, _b in path) == [2.0, 6.0]


# --- P1 regression, Codex review 2026-07-24 --------------------------------------------------
# Flow and depth ignored the declared window: they scanned the entire event buffer while arrival
# rate used 60 s. An old burn kept depth_pull (=> CUT) armed forever, and a stale buy could hold
# "flow positive" long after the live window had turned negative.

def _windowed_case(base, *, stale_kind):
    """Six recent alternating swaps + one STALE event 600 s older than the 60 s window."""
    from sniper.services.memecoin_tick_stream import V2_BURN
    logs = []
    if stale_kind == "burn":
        logs.append({"address": "0xpool", "blockNumber": hex(1), "logIndex": hex(0),
                     "transactionHash": f"0x{1:064x}", "topics": [V2_BURN, "0x" + _word(0xABC)],
                     "data": "0x" + _word(10**6) + _word(10**6)})
    else:   # one huge stale BUY, far outside the window
        logs.append(_v2_swap_log("0xpool", 1, a0_in=10**9, a1_in=0, a0_out=0, a1_out=10**6))
    for i in range(10, 16):     # recent blocks, all SELLS -> current-window flow is negative
        logs.append(_v2_swap_log("0xpool", i, a0_in=0, a1_in=50 + i, a0_out=90 + i, a1_out=0))
    logs.append(_v2_sync_log("0xpool", 15, 10**6, 10**6))
    bt = {1: _iso(base)}
    bt.update({i: _iso(base + 600 + i) for i in range(10, 16)})
    return _decode(logs), bt


def _win_contract():
    return {"chain_id": "4663", "mint": "COIN", "pool": "0xpool", "pool_kind": "v2",
            "position_id": 1, "quote_side": 0, "entry_price": 1.8, "executable_peak": 2.4}


def test_stale_burn_outside_the_window_does_not_pin_depth_pull_forever():
    base = 1_700_000_000
    ev, bt = _windowed_case(base, stale_kind="burn")
    s = compute_state(ev, bt, _win_contract(), cfg=None, now_ts=base + 620)
    assert s.depth_pull_flag is not True


def test_flow_only_counts_events_inside_the_declared_window():
    base = 1_700_000_000
    ev, bt = _windowed_case(base, stale_kind="buy")
    s = compute_state(ev, bt, _win_contract(), cfg=None, now_ts=base + 620)
    assert s.net_flow_quote is None or int(s.net_flow_quote) < 0


def test_blocks_sharing_a_timestamp_stay_in_block_order_even_from_newest_first_input():
    """Codex 2026-07-24: sorting on block_ts alone left tied blocks in caller order, and the
    production reader hands rows back NEWEST-first — so the series ran backwards inside each
    second. 4663 makes ~10 blocks/s, so ties are the common case."""
    from sniper.analysis.exit_features import exec_price_path_per_block
    swaps = [   # newest-first, exactly as _read_pool_ticks supplies them
        {"kind": "v2_swap", "block_no": 2, "block_ts": 100, "amount0": "200", "amount1": "-100"},
        {"kind": "v2_swap", "block_no": 1, "block_ts": 100, "amount0": "100", "amount1": "-100"},
    ]
    path = exec_price_path_per_block(swaps, quote_side=0)
    assert [b for _ts, _px, _n, b in path] == [1, 2]
    assert [px for _ts, px, _n, _b in path] == [1.0, 2.0]

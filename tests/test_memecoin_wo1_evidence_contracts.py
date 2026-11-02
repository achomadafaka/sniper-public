"""WO-1 — per-rule evidence contracts.

Each rule acts on its OWN required feature families, so a fresh family (depth/burn) can CUT while
stale swaps (price/flow/vol) cannot veto it — the missed-rug fix. Today one stale family collapses
the whole state and blocks every rule, including the LP CUT during the exact quiet window rugs
happen in.
"""
import pytest

from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
from sniper.services.memecoin_exit_brain import (
    CUT, DEFER, DERISK, RIDE, TAKE_PROFIT, propose_v5)

_ALL = ("price", "flow", "vol", "depth")


def _prov(**quals):
    """Provenance dict with per-family quality; families omitted default to 'untrusted'."""
    return {f: Provenance("test", quality=quals.get(f, "untrusted")) for f in _ALL}


def _state(*, label=StateLabel.TRENDING, required=_ALL, provenance=None, **kw):
    return MarketState(
        chain_id="solana", mint="M", pool="p", position_id=1, engine_version="v",
        feature_version="v2", state=label, required_families=required,
        provenance=provenance if provenance is not None else _prov(**{f: "trusted" for f in _ALL}),
        **kw)


class _Cfg:
    MEMECOIN_REAL_UNSELLABLE_SWEEPS = 4


# --- candidate_capable per-rule subset semantics ------------------------------------------
def test_candidate_capable_takes_a_per_rule_family_subset():
    s = _state(label=StateLabel.EXIT_PROPOSED, provenance=_prov(depth="trusted"))  # only depth trusted
    assert s.candidate_capable(("depth",)) is True
    assert s.candidate_capable(("price",)) is False
    assert s.candidate_capable(()) is True            # actionable label, no market family needed


def test_explicit_rule_contract_uses_its_family_even_during_warmup():
    warm = _state(label=StateLabel.WARMUP, provenance=_prov(depth="trusted"))
    assert warm.candidate_capable(("depth",)) is True
    assert warm.candidate_capable(("price",)) is False
    assert warm.candidate_capable(()) is True


def test_source_untrusted_remains_a_hard_global_veto():
    state = _state(label=StateLabel.SOURCE_UNTRUSTED, provenance=_prov(depth="trusted"))
    assert state.candidate_capable(("depth",)) is False
    assert state.candidate_capable(()) is False


# --- the missed-rug fix -------------------------------------------------------------------
def test_lp_cut_fires_on_fresh_depth_while_swaps_are_stale():
    """A decoded LP burn (depth trusted) must CUT even when price/flow/vol are stale/untrusted."""
    s = _state(label=StateLabel.EXIT_PROPOSED,
               provenance=_prov(depth="trusted"),   # price/flow/vol untrusted (quiet tape)
               depth_pull_flag=True)
    d = propose_v5({"trail_pct": 30.0}, s, _Cfg())
    assert d.action == CUT and "lp_pull" in d.reason_codes


# --- irrelevant stale family cannot fabricate an action -----------------------------------
def test_take_profit_blocked_when_vol_untrusted():
    """TP requires vol; a TP-shaped state with untrusted vol must not bank — it RIDEs on the
    still-trusted structure+flow instead, never a phantom TP or CUT."""
    s = _state(provenance=_prov(price="trusted", flow="trusted", depth="trusted"),  # vol untrusted
               higher_low_state=True, arrival_accel=2.0, realized_vol=0.31, net_flow_quote="10")
    d = propose_v5({"trail_pct": 30.0}, s, _Cfg())
    assert d.action not in (TAKE_PROFIT, CUT)


def test_structure_cut_requires_price_and_flow():
    broke_untrusted_price = _state(
        provenance=_prov(flow="trusted", vol="trusted", depth="trusted"),  # price untrusted
        nominal_price=1.0, prior_low=1.5, net_flow_quote="-5")
    assert propose_v5({"trail_pct": 30.0}, broke_untrusted_price, _Cfg()).action != CUT
    broke_ok = _state(nominal_price=1.0, prior_low=1.5, net_flow_quote="-5")  # all trusted
    assert propose_v5({"trail_pct": 30.0}, broke_ok, _Cfg()).action == CUT


# --- precedence / reasons preserved -------------------------------------------------------
def test_stale_market_does_not_block_quote_only_unsellable_cut():
    s = _state(label=StateLabel.DATA_STALE)
    d = propose_v5({"trail_pct": 30.0, "exact_quote_misses": 4}, s, _Cfg())
    assert d.action == CUT and "unsellable" in d.reason_codes


def test_stale_market_does_not_block_position_only_time_derisk_with_no_progress():
    s = _state(label=StateLabel.DATA_STALE)
    d = propose_v5({"trail_pct": 30.0, "held_s": 10_000, "coin_band": "nano",
                    "executable_no_progress": True}, s, _Cfg())
    assert d.action == DERISK and "edge_duration_elapsed" in d.reason_codes


def test_time_derisk_fails_closed_without_executable_no_progress_evidence():
    s = _state(label=StateLabel.DATA_STALE)
    d = propose_v5({"trail_pct": 30.0, "held_s": 10_000, "coin_band": "nano"}, s, _Cfg())
    assert d.action != DERISK


def test_fully_trusted_state_is_unchanged_from_pre_wo1():
    """Backward-compat: when every family is trusted, per-rule gating is a no-op — the RIDE/CUT/
    DERISK precedence and reasons are identical to the profile-level gate."""
    ride = _state(higher_low_state=True, net_flow_quote="0")
    assert propose_v5({"trail_pct": 30.0}, ride, _Cfg()).action == RIDE
    derisk = _state(lower_high_state=True, realized_vol=0.35, net_flow_quote="1")
    assert propose_v5({"trail_pct": 30.0}, derisk, _Cfg()).action == DERISK


# --- the hot state builder must stop collapsing on a partially-stale window ----------------
def _feats(**kw):
    base = {"latest_event_ts": None, "latest_swap_ts": None, "latest_depth_ts": None,
            "structure_points": 8, "nominal_price": 1.0, "net_flow_quote": "1",
            "realized_vol": 0.2, "reserve_base": "1000", "depth_pull_flag": None,
            "prior_low": 0.9, "prior_high": 1.2}
    base.update(kw)
    return base


def test_hot_state_fresh_burn_stale_swaps_stays_actionable_depth_only():
    """The hot state builder must NOT collapse to DATA_INSUFFICIENT when swaps are stale but a
    decoded burn keeps depth fresh: the state stays actionable (EXIT_PROPOSED) with only depth
    trusted, so the LP CUT can fire — the missed-rug fix at the state layer."""
    from sniper.services.memecoin_hot_supervisor import hot_state_from_feats
    now = 1000.0
    feats = _feats(latest_event_ts=now - 5, latest_swap_ts=now - 300, latest_depth_ts=now - 5,
                   depth_pull_flag=True)
    st = hot_state_from_feats(feats, now_ts=now, tape_complete=True, stale_s=180.0, min_points=5,
                              mint="M", pool="P", position_id=7)
    assert st.state == StateLabel.EXIT_PROPOSED
    assert st.candidate_capable(("depth",)) is True     # fresh depth -> LP CUT allowed
    assert st.candidate_capable(("price",)) is False     # stale swaps -> price untrusted
    # and the engine actually CUTs on this live-shaped state:
    assert propose_v5({"trail_pct": 30.0}, st, _Cfg()).action == CUT


def test_hot_state_rug_during_warmup_uses_trusted_depth_contract():
    """Structure warmup is irrelevant to a decoded LP removal with fresh trusted depth."""
    from sniper.services.memecoin_hot_supervisor import hot_state_from_feats
    now = 1000.0
    feats = _feats(latest_event_ts=now - 2, latest_depth_ts=now - 2, latest_swap_ts=now - 2,
                   structure_points=1, depth_pull_flag=True)
    st = hot_state_from_feats(feats, now_ts=now, tape_complete=True, stale_s=180.0, min_points=5,
                              mint="M", pool="P", position_id=7)
    assert st.state == StateLabel.WARMUP
    assert st.candidate_capable(("depth",)) is True
    assert propose_v5({"trail_pct": 30.0}, st, _Cfg()).action == CUT


# --- the PULL lane must not collapse either (the lane live until WO-8) ---------------------
def test_pull_lane_fresh_burn_stale_swaps_cuts():
    """compute_state (pull materializer) had the same collapse: any untrusted required family ->
    DATA_INSUFFICIENT -> even a fresh decoded burn could not CUT. Quiet-tape swaps (older than the
    stale bound) + a fresh sol_burn/sol_sync must now yield an actionable state with depth trusted,
    and the engine must CUT on it."""
    import datetime as _dt
    from sniper.services.memecoin_exit_market_state import compute_state

    rows, times = [], {}
    for i in range(8):                       # old swaps: enough structure, stale price/flow/vol
        rows.append({"kind": "sol_swap", "block_no": 100 + i, "maker": None,
                     "amount0": str(-1000 - 10 * i), "amount1": str(500 + 5 * i)})
        rows.append({"kind": "sol_sync", "block_no": 100 + i, "maker": None,
                     "amount0": "90000", "amount1": str(56000 + i)})
        times[100 + i] = f"2026-07-24T00:00:{10 + i:02d}+00:00"
    # fresh LP pull, 400 s after the last swap (swaps stale at the 180 s bound)
    rows.append({"kind": "sol_burn", "block_no": 200, "maker": None,
                 "amount0": "-90000", "amount1": "-56000"})
    rows.append({"kind": "sol_sync", "block_no": 200, "maker": None,
                 "amount0": "10", "amount1": "10"})
    times[200] = "2026-07-24T00:07:00+00:00"
    contract = {"chain_id": "solana", "mint": "M", "pool": "V", "pool_kind": "sol",
                "quote_side": 1, "position_id": 9}
    now_ts = _dt.datetime.fromisoformat("2026-07-24T00:07:05+00:00").timestamp()
    s = compute_state(rows, times, contract, now_ts=now_ts)
    assert s.state == StateLabel.TRENDING            # no profile-wide collapse
    assert s.depth_pull_flag is True
    assert s.candidate_capable(("depth",)) is True   # fresh burn/sync -> depth trusted
    assert s.candidate_capable(("price",)) is False  # stale swaps -> price untrusted
    assert s.candidate_capable() is False            # profile-level gate unchanged (fail-closed)
    d = propose_v5({"trail_pct": 30.0}, s, _Cfg())
    assert d.action == CUT and "lp_pull" in d.reason_codes

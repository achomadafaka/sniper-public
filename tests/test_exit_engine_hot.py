"""Incremental hot engine — event-driven decisions, bounded memory, one-code-path."""
from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
from sniper.services.exit_engine_hot import HotEngineSet, HotPositionEngine
from sniper.services.memecoin_exit_brain import CUT, DEFER, DERISK, RIDE, propose_v5

_TRUSTED = {f: Provenance("pool_ticks", quality="trusted") for f in ("price", "flow", "vol", "depth")}
_REQ = ("price", "flow", "vol", "depth")


def _state_fn(feats, *, now_ts):
    return MarketState(chain_id="solana", mint="C", pool="p", position_id=1, engine_version="v",
                       feature_version="v2", state=StateLabel.TRENDING,
                       required_families=_REQ, provenance=_TRUSTED,
                       nominal_price=feats["nominal_price"], prior_low=feats["prior_low"],
                       higher_low_state=feats["higher_low_state"],
                       lower_high_state=feats["lower_high_state"],
                       realized_vol=feats["realized_vol"], net_flow_quote=feats["net_flow_quote"],
                       arrival_accel=feats["arrival_accel"], depth_pull_flag=feats["depth_pull_flag"])


class _Cfg:
    MEMECOIN_REAL_UNSELLABLE_SWEEPS = 4


def _swap(ts, q, b, kind="sol_swap"):
    return {"kind": kind, "block_ts": ts, "amount0": str(q), "amount1": str(-b)}


def _eng(**kw):
    return HotPositionEngine(1, {"trail_pct": 30.0, **kw}, quote_side=0, state_fn=_state_fn)


def test_decides_on_every_tick_not_on_a_timer():
    e = _eng()
    decisions = [e.on_tick(_swap(i, 100 + i, 50), now_ts=100 + i) for i in range(1, 8)]
    assert all(d is not None for d in decisions)      # a decision per tick, no cadence gate
    assert e.ticks_seen == 7


def test_ring_buffer_is_bounded():
    e = HotPositionEngine(1, {"trail_pct": 30.0}, quote_side=0, state_fn=_state_fn, max_events=10)
    for i in range(50):
        e.on_tick(_swap(i, 100, 50), now_ts=i)
    assert len(e._events) == 10                        # memory cannot grow with uptime


def test_lp_burn_in_the_stream_cuts():
    e = _eng()
    for i in range(1, 6):
        e.on_tick(_swap(i, 100 + i, 50), now_ts=i)
    d = e.on_tick({"kind": "sol_burn", "block_ts": 6, "amount0": "-1", "amount1": "-1"}, now_ts=6)
    assert d.action == CUT and "lp_pull" in d.reason_codes


def test_unusable_tick_is_ignored_and_never_raises():
    e = _eng()
    assert e.on_tick(None, now_ts=1) is None
    assert e.on_tick({"kind": "sol_swap"}, now_ts=1) is None      # no block_ts
    assert e.ticks_seen == 0


def test_engine_set_routes_only_pools_we_hold():
    s = HotEngineSet()
    s.track(("solana", "poolA"), _eng())
    assert s.on_tick(("solana", "poolA"), _swap(1, 100, 50), now_ts=1) is not None
    assert s.on_tick(("solana", "poolZ"), _swap(1, 100, 50), now_ts=1) is None   # not ours
    s.drop(("solana", "poolA"))
    assert s.keys() == set()


def test_uses_the_same_pure_feature_code_as_replay():
    """One-code-path refusal: the engine must not reimplement features. If these drift, replay and
    live disagree and every shadow read becomes meaningless."""
    import inspect
    from sniper.services import exit_engine_hot as m
    src = inspect.getsource(m)
    for fn in ("exec_price_path_per_block", "confirmed_pivots", "realized_vol", "pool_flow",
               "arrival_rate_window", "arrival_acceleration"):
        assert f"from sniper.analysis.exit_features import" in src or fn in src
    assert "def _own_vwap" not in src and "def _own_pivots" not in src


def test_v5_hot_engine_recomputes_elapsed_hold_on_every_tick():
    """Catches freezing held_s at the 30-second book rebuild instead of updating per event.

    Post-remediation (522e288c): age alone has zero authority — the time-DERISK also requires
    explicit `executable_no_progress` evidence, so the contract supplies it and the rule's firing
    edge still proves held_s refreshed on the tick."""
    e = HotPositionEngine(
        1, {"trail_pct": 30.0, "coin_band": "nano", "opened_ts": 0.0,
            "executable_no_progress": True}, quote_side=0,
        state_fn=_state_fn, decision_fn=propose_v5, engine_version="exit-brain-v5")
    assert e.on_tick(_swap(1, 100, 50), now_ts=1523).action != DERISK
    assert e.on_tick(_swap(2, 101, 50), now_ts=1524).action == DERISK
    assert e.contract["held_s"] == 1524
    assert e.engine_version == "exit-brain-v5"


def test_v5_time_derisk_has_no_authority_on_age_alone():
    """Pin the remediation contract: without explicit executable_no_progress evidence, elapsed age
    NEVER derisks — age alone is not staleness (translation-gates 07-16)."""
    e = HotPositionEngine(
        1, {"trail_pct": 30.0, "coin_band": "nano", "opened_ts": 0.0}, quote_side=0,
        state_fn=_state_fn, decision_fn=propose_v5, engine_version="exit-brain-v5")
    assert e.on_tick(_swap(1, 100, 50), now_ts=99999).action != DERISK


def test_hot_structure_cut_uses_same_two_event_controller_hysteresis():
    def structure_state(feats, *, now_ts):
        return MarketState(
            chain_id="solana", mint="C", pool="p", position_id=1, engine_version="v",
            feature_version="v4", state=StateLabel.TRENDING,
            required_families=("price", "flow"),
            provenance={f: Provenance("pool_ticks", quality="trusted")
                        for f in ("price", "flow")},
            latest_event_id=feats["latest_event_id"],
            latest_event_time=f"2026-07-25T00:00:0{int(now_ts)}Z",
            latest_event_block_no=feats["latest_event_block_no"],
            latest_event_log_index=feats["latest_event_log_index"],
            nominal_price=0.8, prior_low=1.0,
            net_flow_quote="-1", source_event_time=f"2026-07-25T00:00:0{int(now_ts)}Z")

    engine = HotPositionEngine(
        1, {"trail_pct": 30.0}, quote_side=0, state_fn=structure_state,
        decision_fn=propose_v5, engine_version="exit-brain-v5")
    first = {**_swap(1, 100, 50), "block_no": 1, "tx_sig": "sig1", "log_index": 0}
    second = {**_swap(2, 100, 50), "block_no": 2, "tx_sig": "sig2", "log_index": 0}

    assert engine.on_tick(first, now_ts=1, cfg=_Cfg()).action == DEFER
    assert engine.on_tick(second, now_ts=2, cfg=_Cfg()).action == CUT


def test_same_slot_decoded_swaps_both_advance_hot_hysteresis():
    def structure_state(feats, *, now_ts):
        return MarketState(
            chain_id="solana", mint="C", pool="p", position_id=1, engine_version="v",
            feature_version="v4", state=StateLabel.TRENDING,
            required_families=("price", "flow"),
            provenance={f: Provenance("decoded", quality="trusted")
                        for f in ("price", "flow")},
            latest_event_id=feats["latest_event_id"],
            latest_event_time="2026-07-25T00:00:01Z",
            latest_event_block_no=feats["latest_event_block_no"],
            latest_event_log_index=feats["latest_event_log_index"],
            nominal_price=0.8, prior_low=1.0, net_flow_quote="-1",
            source_event_time="2026-07-25T00:00:01Z")

    engine = HotPositionEngine(
        1, {"trail_pct": 30.0, "chain_id": "solana", "pool": "p"}, quote_side=0,
        state_fn=structure_state, decision_fn=propose_v5)
    decision = engine.corroborate(
        {10}, [
            {**_swap(1, 100, 50), "block_no": 10, "tx_sig": "sigA", "log_index": 0},
            {**_swap(1, 100, 50), "block_no": 10, "tx_sig": "sigB", "log_index": 0},
        ], now_ts=1, cfg=_Cfg())

    assert decision.action == CUT
    controller = engine.contract["controller_state"]
    assert controller.consecutive_break_events == 2
    assert controller.controller_last_event_id == "solana|p|sigB|0"


def test_history_warmup_adds_ticks_without_reducing_controller_state():
    from sniper.analysis.exit_controller import ControllerState
    initial = ControllerState(
        controller_revision=4, controller_last_event_id="live",
        controller_last_event_block=100, controller_last_event_log_index=0,
        consecutive_break_events=1)
    engine = HotPositionEngine(
        1, {"trail_pct": 30.0, "controller_state": initial}, quote_side=0,
        state_fn=_state_fn)
    for block in range(1, 10):
        assert engine.add_tick({**_swap(block, 100, 50), "block_no": block,
                                "tx_sig": f"old{block}", "log_index": 0})
    assert engine.contract["controller_state"] == initial
    assert engine.last_decision is None

"""WO-2 — transaction-grounded sale authority on the hot lane.

Vault-delta rows (tx_sig=None) fabricate burns (fee claims, multi-tx slots net to a burn-shaped
delta) and can corrupt flow sign. They may WAKE + PREWARM but must NEVER authorize a sale. Before a
CUT/DERISK/TAKE_PROFIT reaches on_decision, the supervisor decodes the triggering transaction(s) and
re-proposes on the decoded (tx_sig-set) rows. Decode failure fails closed to the pull lane.

Decode scope (ultimate-engine invariant):
  - every provisional slot that can contribute to a sale's current features is decoded;
  - enumeration must cross below the oldest required slot before coverage is complete;
  - unsellable CUT / time-DERISK: NOT vault-delta evidence -> authorized without decode.

These tests isolate the corroboration ORCHESTRATION with a fake engine set; vault_rows decoding is
covered by its own tests.
"""
from sniper.services.memecoin_exit_brain import CandidateDecision
from sniper.services.memecoin_hot_supervisor import HotSupervisor, SolanaDecodeContext

KEY = ("solana", "CV")
_SLOT = 500


def _dec(action, *reasons):
    return CandidateDecision(action=action, reason_codes=tuple(reasons))


class _FakeEngines:
    """on_tick returns `provisional` for tx_sig-None rows; corroborate returns `decoded`."""
    def __init__(self, provisional, decoded):
        self.provisional, self.decoded = provisional, decoded
        self.corroborated = None
        self.window = {_SLOT: 987.0}      # provisional flow-window slots the supervisor must ground

    def on_tick(self, key, row, *, now_ts, cfg=None):
        return self.provisional

    def engine(self, key):
        return self

    def window_slots(self, **_kw):
        return dict(self.window)

    def provisional_slots(self):
        return dict(self.window)

    def corroborate(self, key, slots, decoded_rows, *, now_ts, cfg=None):
        self.corroborated = (key, slots, tuple(decoded_rows))
        return self.decoded


def _ctx(*, sigs=(({"signature": "sigA", "slot": _SLOT}),), tx=None, get_sigs=None, get_tx=None):
    return SolanaDecodeContext(
        coin_mint="MINT", coin_vault="CV",
        get_sigs_fn=get_sigs or (lambda _v, _p: list(sigs)),
        get_tx_fn=get_tx or (lambda _s: tx if tx is not None else {"decoded": True}))


def _sup(engines, ctx, *, on_decision):
    return HotSupervisor(
        engines, now_fn=lambda: 1000.0, on_decision=on_decision,
        decode_ctx_fn=lambda _key: ctx)


def _row(**kw):
    base = {"chain_id": "solana", "pool": "CV", "block_no": _SLOT, "tx_sig": None,
            "amount0": "-100", "amount1": "-50", "kind": "sol_burn"}
    base.update(kw)
    return base


def _run(provisional, decoded, ctx):
    calls = []
    sup = _sup(_FakeEngines(provisional, decoded), ctx,
               on_decision=lambda *a: calls.append(a))
    sup.on_ws_row(_row())
    return calls


# --- vault-delta sale must be decode-corroborated -----------------------------------------
def test_fabricated_burn_not_authorized_when_decode_refutes():
    """Provisional net-delta looks like a burn -> CUT, but the decoded tx is a swap (no depth pull)
    -> the re-proposal is not a sale -> on_decision is NOT called."""
    calls = _run(_dec("CUT", "cut", "lp_pull"), _dec("DEFER_TO_LEGACY", "no_signal"), _ctx())
    assert calls == []


def test_decoded_real_burn_authorizes_cut():
    calls = _run(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull"), _ctx())
    assert len(calls) == 1


def test_decode_failure_fails_closed():
    """get_tx returns None (RPC outage) -> never a delta-authorized sale; pull lane covers it."""
    ctx = _ctx(get_tx=lambda _s: None)
    calls = _run(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull"), ctx)
    assert calls == []


def test_missing_decode_context_fails_closed():
    calls = []
    sup = _sup(_FakeEngines(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull")),
               None, on_decision=lambda *a: calls.append(a))
    sup.on_ws_row(_row())
    assert calls == []


# --- decode scope by rule -----------------------------------------------------------------
def test_structure_cut_over_sig_cap_fails_closed():
    """break_confirmed decodes the whole flow window; more than the cap of sigs -> fail closed."""
    many = [{"signature": f"s{i}", "slot": _SLOT} for i in range(11)]   # > cap (10)
    ctx = _ctx(sigs=many)
    calls = _run(_dec("CUT", "cut", "break_confirmed", "flow_negative"),
                 _dec("CUT", "cut", "break_confirmed"), ctx)
    assert calls == []


def test_unsellable_cut_authorized_without_decode():
    """unsellable evidence is quote-failure, not a vault delta -> no decode, authorized as-is even
    with no decode context."""
    calls = []
    sup = _sup(_FakeEngines(_dec("CUT", "cut", "unsellable", "misses:4"), None),
               None, on_decision=lambda *a: calls.append(a))
    sup.on_ws_row(_row(kind="sol_swap", tx_sig=None))
    assert len(calls) == 1


def test_time_derisk_authorized_without_decode():
    calls = []
    sup = _sup(_FakeEngines(_dec("DERISK", "derisk", "edge_duration_elapsed"), None),
               None, on_decision=lambda *a: calls.append(a))
    sup.on_ws_row(_row())
    assert len(calls) == 1


# --- the accepted RPC tradeoff: fail-closed leaves the gap open for the pull lane ----------
def test_fail_closed_sale_leaves_the_decision_gap_open_for_pull():
    """Operator-accepted tradeoff: a decode outage must NOT reset the decision gap, so the 60s
    pull lane covers the genuine rug instead of the hot lane silently swallowing it."""
    sup = _sup(_FakeEngines(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull")),
               _ctx(get_tx=lambda _s: None), on_decision=lambda *a: None)
    sup.on_ws_row(_row())
    assert sup.gap.needs_pull_fallback(1000.0) is True


def test_confirmed_sale_resets_the_decision_gap():
    sup = _sup(_FakeEngines(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull")),
               _ctx(), on_decision=lambda *a: None)
    sup.on_ws_row(_row())
    assert sup.gap.needs_pull_fallback(1000.0) is False


# --- non-sale decisions are unchanged (WO-2 only gates SALES) ------------------------------
def test_ride_and_defer_pass_through_without_decode():
    """Non-sale decisions keep the pre-WO-2 contract: on_decision is still called and no decode
    runs. Only CUT/DERISK/TAKE_PROFIT are corroborated."""
    for d in (_dec("RIDE", "ride"), _dec("DEFER_TO_LEGACY", "no_signal")):
        eng = _FakeEngines(d, None)
        calls = []
        sup = _sup(eng, _ctx(), on_decision=lambda *a: calls.append(a))
        sup.on_ws_row(_row())
        assert len(calls) == 1
        assert eng.corroborated is None          # no decode attempted for a non-sale


# --- review fixes: window decode must replace EVERY slot, keep its ts, cover all slots -----
def _sol_tx(coin_d, quote_d):
    """Minimal getTransaction fixture: pool owner P holds coin vault CV + WSOL vault QV."""
    from sniper.services.memecoin_solana_amounts import WSOL
    def bal(idx, mint, amt):
        return {"accountIndex": idx, "mint": mint, "owner": "P",
                "uiTokenAmount": {"amount": str(amt)}}
    return {"transaction": {"message": {"accountKeys": ["F", "CV", "QV"]}},
            "meta": {"preTokenBalances": [bal(1, "MINT", 1000), bal(2, WSOL, 500)],
                     "postTokenBalances": [bal(1, "MINT", 1000 + coin_d),
                                           bal(2, WSOL, 500 + quote_d)]}}


def test_real_engine_decodes_structure_pending_and_emits_it_for_persistence():
    """A provisional Solana swap has no event identity, so the first break is pending. The pending
    decision itself must trigger decode, then reach the funded callback with count=1; otherwise a
    second fast event starts from persisted zero and cannot CUT at the registered second event."""
    from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
    from sniper.services.exit_engine_hot import HotEngineSet, HotPositionEngine
    from sniper.services.memecoin_exit_brain import propose_v5

    trusted = {f: Provenance("decoded", quality="trusted") for f in ("price", "flow")}

    def state_fn(feats, *, now_ts):
        return MarketState(
            chain_id="solana", mint="MINT", pool="CV", position_id=1,
            engine_version="exit-brain-v2", feature_version="v4", state=StateLabel.TRENDING,
            required_families=("price", "flow"), provenance=trusted,
            latest_event_id=feats.get("latest_event_id"),
            latest_event_time="1970-01-01T00:16:27+00:00",
            latest_event_block_no=feats.get("latest_event_block_no"),
            latest_event_log_index=feats.get("latest_event_log_index"),
            nominal_price=0.8, prior_low=1.0, net_flow_quote="-1",
            source_event_time="1970-01-01T00:16:27+00:00")

    engine = HotPositionEngine(
        1, {"trail_pct": 30.0, "chain_id": "solana", "pool": "CV"}, quote_side=1,
        state_fn=state_fn, decision_fn=propose_v5)
    engines = HotEngineSet(); engines.track(KEY, engine)
    calls = []
    sup = _sup(
        engines, _ctx(tx=_sol_tx(-100, 50)),
        on_decision=lambda *args: calls.append(args))

    decision = sup.on_ws_row(_row(kind="sol_swap", amount0="-100", amount1="50"))

    assert "structure_hysteresis_pending" in decision.reason_codes
    assert len(calls) == 1
    emitted = calls[0][1]
    assert "structure_hysteresis_pending" in emitted.reason_codes
    assert engine.contract["controller_state"].consecutive_break_events == 1
    assert engine.contract["controller_state"].controller_last_event_id == "solana|CV|sigA|0"


def test_structure_cut_decodes_every_provisional_feature_slot_with_original_ts():
    """The flow-window decode must ground EVERY provisional slot in the window and stamp each
    decoded row with its slot's ORIGINAL event time — stamping `now` would time-travel older slots
    into the arrival/flow windows; replacing only the trigger slot would double-count the rest."""
    eng = _FakeEngines(_dec("CUT", "cut", "break_confirmed", "flow_negative"),
                       _dec("CUT", "cut", "break_confirmed"))
    eng.window = {_SLOT: 987.0, 499: 985.0}
    ctx = _ctx(sigs=[{"signature": "sA", "slot": _SLOT}, {"signature": "sB", "slot": 499}],
               get_tx=lambda _s: _sol_tx(-10, +6))
    calls = []
    sup = _sup(eng, ctx, on_decision=lambda *a: calls.append(a))
    sup.on_ws_row(_row())
    key, slots, rows = eng.corroborated
    assert set(slots) == {_SLOT, 499}                      # every window slot replaced
    ts_by_slot = {r["block_no"]: r["block_ts"] for r in rows}
    assert ts_by_slot[499] == 985.0 and ts_by_slot[_SLOT] == 987.0
    assert len(calls) == 1                                 # decoded sale authorized


def test_take_profit_decodes_every_provisional_feature_slot():
    """TP consumes structure, volatility, and arrival history; grounding only its trigger slot leaves
    the other sale-authorizing inputs as vault-delta guesses."""
    eng = _FakeEngines(_dec("TAKE_PROFIT", "take_profit", "climax"),
                       _dec("TAKE_PROFIT", "take_profit", "climax"))
    eng.window = {_SLOT: 987.0, 499: 985.0}
    ctx = _ctx(sigs=[{"signature": "sA", "slot": _SLOT}, {"signature": "sB", "slot": 499}],
               get_tx=lambda _s: _sol_tx(-10, +6))
    calls = []
    sup = _sup(eng, ctx, on_decision=lambda *a: calls.append(a))
    sup.on_ws_row(_row())
    assert set(eng.corroborated[1]) == {_SLOT, 499}
    assert len(calls) == 1


def test_signature_scan_paginates_past_oldest_target_slot_before_authorizing():
    """Seeing one signature in a target slot is not completeness: another signature from that same
    slot may be on the next page. The scan must cross to an older slot before authorizing."""
    eng = _FakeEngines(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull"))
    eng.window = {_SLOT: 987.0}
    pages = {
        None: [
            {"signature": "new2", "slot": 502},
            {"signature": "new1", "slot": 501},
            {"signature": "target-a", "slot": _SLOT},
        ],
        "target-a": [
            {"signature": "target-b", "slot": _SLOT},
            {"signature": "older", "slot": 499},
        ],
    }
    requested = []
    def get_sigs(_vault, params):
        requested.append(dict(params))
        return pages[params.get("before")]
    ctx = _ctx(get_sigs=get_sigs, get_tx=lambda _s: _sol_tx(-10, +6))
    calls = []
    sup = HotSupervisor(
        eng, now_fn=lambda: 1000.0, on_decision=lambda *a: calls.append(a),
        decode_ctx_fn=lambda _key: ctx, decode_page_limit=3)
    sup.on_ws_row(_row())
    assert [p.get("before") for p in requested] == [None, "target-a"]
    assert len(eng.corroborated[2]) == 4       # sync + swap for both target-slot transactions
    assert len(calls) == 1


def test_signature_scan_page_bound_fails_closed_to_pull_lane():
    """A busy vault/RPC must not hold the WS consumer through an effectively unbounded scan."""
    eng = _FakeEngines(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull"))
    calls, requested = [], []

    def get_sigs(_vault, params):
        requested.append(dict(params))
        n = len(requested)
        return [{"signature": f"new-{n}", "slot": _SLOT + n}]

    sup = HotSupervisor(
        eng, now_fn=lambda: 1000.0, on_decision=lambda *a: calls.append(a),
        decode_ctx_fn=lambda _key: _ctx(get_sigs=get_sigs),
        decode_page_limit=1, decode_max_pages=2)
    sup.on_ws_row(_row())
    assert len(requested) == 2
    assert calls == []
    assert sup.gap.needs_pull_fallback(1000.0) is True


def test_structure_cut_uncovered_window_slot_fails_closed():
    """A provisional window slot with no resolvable signature keeps its (possibly netted-corrupt)
    evidence -> the sale must fail closed to the pull lane, not authorize on a half-grounded sum."""
    eng = _FakeEngines(_dec("CUT", "cut", "break_confirmed", "flow_negative"),
                       _dec("CUT", "cut", "break_confirmed"))
    eng.window = {_SLOT: 987.0, 499: 985.0}
    ctx = _ctx(sigs=[{"signature": "sA", "slot": _SLOT}])   # slot 499 has no sig
    calls = []
    sup = _sup(eng, ctx, on_decision=lambda *a: calls.append(a))
    sup.on_ws_row(_row())
    assert calls == []


def test_ring_corroborate_replaces_all_slots_no_double_count():
    """Ring-level: corroborate(slots, ...) removes the provisional rows for EVERY passed slot, so
    decoded rows are never summed beside their netted originals and a fake burn cannot linger."""
    from sniper.services.exit_engine_hot import HotPositionEngine
    eng = HotPositionEngine(1, {}, quote_side=1, state_fn=lambda f, *, now_ts: None)
    eng.add_tick({"kind": "sol_swap", "block_no": 10, "tx_sig": None, "block_ts": 990.0,
                  "amount0": "-100", "amount1": "50"})
    eng.add_tick({"kind": "sol_burn", "block_no": 11, "tx_sig": None, "block_ts": 995.0,
                  "amount0": "-90", "amount1": "-45"})
    decoded = [
        {"kind": "sol_swap", "block_no": 10, "tx_sig": "sA", "block_ts": 990.0,
         "amount0": "-100", "amount1": "50"},
        {"kind": "sol_sync", "block_no": 11, "tx_sig": "sB", "block_ts": 995.0,
         "amount0": "900", "amount1": "450"},               # the "burn" was a fee claim: sync only
    ]
    eng.corroborate({10, 11}, decoded, now_ts=1000.0)
    assert [e for e in eng._events if e.get("tx_sig") is None] == []
    feats = eng.features(now_ts=1000.0)
    assert (feats["buy_n"] or 0) + (feats["sell_n"] or 0) == 1   # decoded swap counted exactly once
    assert feats["depth_pull_flag"] is None                       # fake burn gone for good


def test_ring_corroborate_replaces_previously_decoded_identity_instead_of_duplicating():
    from sniper.services.exit_engine_hot import HotPositionEngine
    eng = HotPositionEngine(1, {}, quote_side=1, state_fn=lambda f, *, now_ts: None)
    row = {"kind": "sol_swap", "block_no": 10, "tx_sig": "same", "block_ts": 990.0,
           "amount0": "-100", "amount1": "50"}
    eng.corroborate({10}, [row], now_ts=1000.0)
    eng.corroborate({10}, [row], now_ts=1000.0)
    assert [e for e in eng._events if (e.get("block_no"), e.get("tx_sig")) == (10, "same")] == [row]
    assert eng.features(now_ts=1000.0)["buy_n"] == 1


def test_ring_corroborate_restores_event_order_before_computing_current_reserve():
    from sniper.services.exit_engine_hot import HotPositionEngine

    eng = HotPositionEngine(1, {}, quote_side=1, state_fn=lambda f, *, now_ts: None)
    decoded_newest_first = [
        {"kind": "sol_sync", "block_no": 11, "tx_sig": "new", "log_index": 1,
         "block_ts": 991.0, "amount0": "1100", "amount1": "550"},
        {"kind": "sol_sync", "block_no": 10, "tx_sig": "old", "log_index": 1,
         "block_ts": 990.0, "amount0": "1000", "amount1": "500"},
    ]
    eng.corroborate({10, 11}, decoded_newest_first, now_ts=1000.0)
    assert eng.features(now_ts=1000.0)["reserve_base"] == "1100"


def test_async_hot_callback_keeps_event_loop_alive_while_decode_rpc_blocks():
    import asyncio
    import threading

    started = threading.Event()
    release = threading.Event()
    def blocking_tx(_sig):
        started.set()
        release.wait(1.0)
        return _sol_tx(-10, +6)

    async def scenario():
        eng = _FakeEngines(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull"))
        sup = _sup(eng, _ctx(get_tx=blocking_tx), on_decision=lambda *a: None)
        task = asyncio.create_task(sup.on_ws_row_async(_row()))
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)                         # must run while RPC thread is blocked
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())


def test_async_decode_runs_off_loop_but_engine_corroboration_returns_to_owner_thread():
    import asyncio
    import threading

    owner = threading.get_ident()
    seen = {}

    class ThreadChecked(_FakeEngines):
        def corroborate(self, key, slots, decoded_rows, *, now_ts, cfg=None):
            seen["corroborate"] = threading.get_ident()
            return super().corroborate(
                key, slots, decoded_rows, now_ts=now_ts, cfg=cfg)

    def get_tx(_sig):
        seen["decode"] = threading.get_ident()
        return _sol_tx(-10, +6)

    async def scenario():
        eng = ThreadChecked(_dec("CUT", "cut", "lp_pull"), _dec("CUT", "cut", "lp_pull"))
        sup = _sup(eng, _ctx(get_tx=get_tx), on_decision=lambda *a: None)
        await sup.on_ws_row_async(_row())

    asyncio.run(scenario())
    assert seen["decode"] != owner
    assert seen["corroborate"] == owner

"""GRADUATION DIRECT (operator order via Alpha, 2026-10-07): a copied curve buy that cannot land because
the curve completed is bought on the coin's canonical PumpSwap pool as soon as the pool exists, priced
from the pool and capped on the SOURCE's curve trade. Real BLASTAR data (tests/fixtures/
pump_graduation_blastar.json): the source left 22.9T atomic tokens on the curve, two other buys in the
same slot completed it, our send reverted BondingCurveComplete at S+1, the pool appeared at S+10."""
import dataclasses
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps
from sniper.execution.solana_native_entry_quote import read_native_state
from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
from sniper.services import memecoin_slot_graduation as G
from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_slot_signal as S

FX = json.loads((Path(__file__).parent / "fixtures" / "pump_graduation_blastar.json").read_text())
MINT, POOL = FX["mint"], FX["pool"]
USER = "4E1f85HhyYQ8LwWjxsGd7JuemNkBC8hjkDN3VHo98Q3o"
BLOCKHASH = "11111111111111111111111111111111"


def _slot_buy(line, **over):
    hit = S._curve(anchor_event_bytes(line), b"", any_user=True)
    fields = dict(source_user_id="u1", handle="degen", owner="OWNER", signature="SIG", slot=454237774,
                  token_program=None, trade_event_log=line,
                  recv_monotonic=time.monotonic(), recv_utc="2026-10-07T13:34:33.379+00:00", **hit)
    fields.update(over)
    return S.SlotBuy(**fields)


def _replay_rpc():
    calls = list(FX["native_state_calls"])

    def rpc(method, params):
        call = calls.pop(0)
        assert call["method"] == method and call["params"][0] == params[0]
        return call["result"]
    return rpc


def _state():
    return read_native_state(MINT, _replay_rpc())


# --- the trigger --------------------------------------------------------------------------------

def test_only_a_source_buy_that_leaves_no_real_tokens_completed_the_curve():
    done = _slot_buy(FX["completing"]["trade_event_log"])
    near = _slot_buy(FX["source"]["trade_event_log"])
    assert done.real_token_reserves == 0 and G.curve_completed(done)
    assert near.real_token_reserves == 22_922_465_421_202 and not G.curve_completed(near)
    assert not G.curve_completed(dataclasses.replace(done, virtual_sol_reserves=0))   # a quote curve
    assert not G.curve_completed(dataclasses.replace(done, venue="pump_amm"))


# --- the pool ------------------------------------------------------------------------------------

def test_the_watch_accepts_only_the_canonical_migrated_wsol_pool_with_its_own_vaults():
    state = _state()
    assert state["account"] == POOL == G.canonical_pool(MINT)
    assert G.pool_refusal(state, MINT) is None
    assert state["fees"] == (20, 5, 95) and state["base_token_program"] == pcb.TOKEN_2022_PROGRAM
    facts = state["pool_facts"]
    lookalike = dict(state, pool_facts=dict(facts, creator=USER))
    assert G.pool_refusal(lookalike, MINT) == "not_pump_migrated"
    assert G.pool_refusal(dict(state, pool_facts=dict(facts, pool_base_token_account=USER)), MINT) == "vault_mismatch"
    assert G.pool_refusal(dict(state, account=USER), MINT) == "not_canonical_pool"
    assert G.pool_refusal(state, "So11111111111111111111111111111111111111112") == "not_canonical_pool"
    assert G.pool_refusal({"reason": "no_curve_or_canonical_pool"}, MINT) in G.TRANSIENT
    assert G.pool_refusal({"venue": "curve"}, MINT) in G.TRANSIENT


# --- the build: pool prices the clip, the source's curve trade prices the caps ------------------

def _builder(clock):
    b = ps.SlotLanePumpSwapBuilder(user=USER, jup_call=lambda *a, **k: None, rpc=lambda *a: None, clock=clock)
    b._buyback = ps.buyback_fee_recipient_from_global_config(FX["global_config_b64"])
    b._protocol_recipient = ps.protocol_fee_recipient_from_global_config(FX["global_config_b64"])
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": pcb.clip_atomic(50.0),
              "min_out_lamports": 270_000_000, "out_lamports": 272_000_000, "fetched_at": clock()}
    return b


def _grad(state):
    return G.payload(state, trigger="bonding_curve_complete", offered_mono=0.0, read_ms=12.0, now_mono=1.0)


def _at_premium(state, premium, sol_usd):
    """The pool state with its quote reserve set so the spot price is `premium` over the source's
    post-trade curve price (the base reserve is the real one)."""
    src = pcb.curve_source_price_usd(pcb.parse_trade_event(FX["source"]["trade_event_log"]), sol_usd)
    base = state["base_reserve"]
    quote = int(src * (1 + premium) / sol_usd * base / 1e6 * 1e9) - state["virtual_quote"]
    return dict(state, quote_reserve=quote), src


@pytest.fixture
def hard_cap(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})


def test_a_graduation_buy_prices_on_the_pool_and_caps_on_the_source_curve(hard_cap):
    clock = time.monotonic
    sol_usd = 185.0
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=sol_usd)
    state, src = _at_premium(_state(), 0.10, sol_usd)
    buy = _slot_buy(FX["source"]["trade_event_log"], graduation=_grad(state))
    b = _builder(clock)
    msg, detail = b.build(buy, 50.0, snap)
    assert msg is not None, detail
    info = b.last_build
    expected = ps.expected_base_out(270_000_000, state["base_reserve"], state["quote_reserve"],
                                    state["virtual_quote"], 120)
    curve = pcb.parse_trade_event(FX["source"]["trade_event_log"])
    average = pcb.curve_source_average_usd(curve, src)
    want = pcb.capped_min_out(expected, 30.0, source_price_usd=src, source_cost_usdc_atomic=pcb.clip_atomic(50.0),
                              source_price_cap_pct=30.0, base_decimals=6, source_average_price_usd=average)
    assert info["expected_tokens_out"] == expected and info["min_tokens_out"] == want[0]
    assert info["source_average_price_usd"] == pytest.approx(average)
    assert detail["target_mint"] == MINT and detail["route_target"].endswith(POOL)
    # The caps are against the CURVE: a pool already 40% over the source refuses before signing,
    # though the pool's own price would pass any check made against itself.
    over, _ = _at_premium(_state(), 0.40, sol_usd)
    msg, why = b.build(dataclasses.replace(buy, graduation=_grad(over)), 50.0, snap)
    assert msg is None and "source-price cap refuses" in why


def test_a_graduation_build_needs_the_protocol_fee_recipient(hard_cap):
    b = _builder(time.monotonic)
    b._protocol_recipient = None
    buy = _slot_buy(FX["source"]["trade_event_log"], graduation=_grad(_state()))
    assert b.build(buy, 50.0, SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=185.0)) \
        == (None, "no_protocol_fee_recipient")


def test_the_protocol_fee_recipient_offset_names_live_buys_recipients():
    import base64
    raw = base64.b64decode(FX["global_config_b64"])
    from solders.pubkey import Pubkey
    listed = {str(Pubkey.from_bytes(raw[57 + 32 * i:89 + 32 * i])) for i in range(8)}
    assert ps.protocol_fee_recipient_from_global_config(FX["global_config_b64"]) in listed
    for line in FX["first_pool_buy_logs"]:
        assert ps.parse_buy_event(line)["protocol_fee_recipient"] in listed


def test_dispatch_routes_a_graduation_buy_to_the_pumpswap_builder():
    seen = []
    builders = {"pump_curve": SimpleNamespace(build=lambda *a, **k: seen.append("curve") or (None, "x")),
                "pump_amm": SimpleNamespace(build=lambda *a, **k: seen.append("amm") or (None, "x"))}
    build = L.dispatch_build(builders)
    buy = _slot_buy(FX["source"]["trade_event_log"])
    build(buy, 50.0, None)
    build(dataclasses.replace(buy, graduation={"pool": POOL}), 50.0, None)
    assert seen == ["curve", "amm"]


# --- the watcher ---------------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


def _watcher(reads, *, live=True):
    clock, put, woke, lines = _Clock(), [], [], []
    pending = L.PendingLegs()
    seq = list(reads)

    def read(mint):
        out = seq.pop(0)
        if isinstance(out, Exception):
            raise out
        return out
    w = G.GraduationWatcher(read_fn=read, requeue_fn=put.append, wake_fn=lambda: woke.append(1),
                            pending=pending, live=live, log=lines.append, clock=clock,
                            deadline_s=6.0, poll_s=0.4)
    return w, clock, put, woke, lines, pending


def test_the_watch_polls_until_the_pool_exists_then_hands_the_buy_back_once():
    absent = {"reason": "no_curve_or_canonical_pool"}
    w, clock, put, woke, lines, pending = _watcher([absent, RuntimeError("timeout"), _state()])
    buy = _slot_buy(FX["source"]["trade_event_log"], mint=MINT)
    assert w.offer(buy, trigger="bonding_curve_complete") is True and pending.holds("SIG", clock.t)
    w.run_once()
    assert put == []
    clock.t += 0.2
    w.run_once()                                      # not due yet: no read
    assert w.stats["reads"] == 1
    clock.t += 0.3
    w.run_once()                                      # a read error is transient
    clock.t += 0.5
    w.run_once()
    assert len(put) == 1 and put[0].graduation["pool"] == POOL and put[0].graduation["trigger"] == "bonding_curve_complete"
    assert pending.holds("SIG", clock.t) and woke == []          # the lane claims it, not the watcher
    w.run_once()
    assert len(put) == 1 and w.stats["reads"] == 3


def test_the_migration_listener_makes_the_watch_read_at_once():
    w, clock, put, woke, lines, pending = _watcher([{"reason": "no_curve_or_canonical_pool"}, _state()])
    w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="bonding_curve_complete")
    w.run_once()
    clock.t += 0.05
    w.notify("SomeOtherPool1111111111111111111111111111111")
    w.run_once()
    assert put == []
    w.notify(POOL)
    w.run_once()
    assert len(put) == 1 and w.stats["notified"] == 1


def test_migration_notice_during_an_absent_read_is_not_lost():
    w, clock, put, woke, lines, pending = _watcher([])
    reads = []

    def read(mint):
        reads.append(mint)
        if len(reads) == 1:
            clock.t += 0.05
            w.notify(POOL)  # Listener receives migration while the older RPC read is in flight.
            return {"reason": "no_curve_or_canonical_pool"}
        return _state()

    w.read_fn = read
    assert w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="source_completed")
    w.run_once()
    w.run_once()  # Event wakes the watcher without advancing the 400 ms poll clock.
    assert len(put) == 1 and len(reads) == 2
    w.run_once()
    assert len(put) == 1 and len(reads) == 2


def test_no_pool_by_the_deadline_releases_the_hold_and_wakes_the_on_chain_lane():
    absent = {"reason": "no_curve_or_canonical_pool"}
    w, clock, put, woke, lines, pending = _watcher([absent] * 20)
    w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="source_completed")
    for _ in range(20):
        w.run_once()
        clock.t += 0.4
    assert put == [] and woke == [1] and not pending.holds("SIG", clock.t)
    assert pending.release_kind("SIG", clock.t) == "expired" and w.stats["expired"] == 1
    assert "-> expired" in lines[-1]


def test_a_lookalike_pool_ends_the_watch_at_once():
    state = _state()
    bad = dict(state, pool_facts=dict(state["pool_facts"], creator=USER))
    w, clock, put, woke, lines, pending = _watcher([bad])
    w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="bonding_curve_complete")
    w.run_once()
    assert put == [] and woke == [1] and "not_pump_migrated" in lines[-1]


# SOL02 (engineering review 2026-10-08): read_native_state tags an incomplete PumpSwap read with its
# venue and a reason and no account; the identity checks read the missing account as a wrong pool.

@pytest.mark.parametrize("reason", ["pool_or_fee_unreadable", "vault_unreadable"])
def test_a_tagged_unreadable_pool_read_keeps_the_watch_going(reason):
    w, clock, put, woke, lines, pending = _watcher([{"venue": "pumpswap", "reason": reason, "slot": 1}, _state()])
    w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="bonding_curve_complete")
    w.run_once()
    assert woke == [] and pending.holds("SIG", clock.t)
    clock.t += 0.5
    w.run_once()
    assert w.stats["reads"] == 2 and len(put) == 1 and put[0].graduation["pool"] == POOL


@pytest.mark.parametrize("reason", ["pool_or_fee_unreadable", "vault_unreadable"])
def test_a_tagged_unreadable_read_is_transient_for_wsol_and_quote_pools(reason):
    state = {"venue": "pumpswap", "reason": reason, "slot": 1}
    assert G.pool_refusal(state, MINT) == reason and reason in G.TRANSIENT
    assert G.pool_refusal(state, MINT, quote_mint="WLDTE73PPdfFFjhpBz8Yuhvx5N45gvgEgyMXqwa3rFn") == reason


def test_an_unmodelled_pool_fee_stays_terminal_under_its_own_reason():
    assert G.pool_refusal({"venue": "pumpswap", "reason": "fee_unmodelled", "slot": 1}, MINT) == "fee_unmodelled"
    assert "fee_unmodelled" not in G.TRANSIENT


def test_tagged_unreadable_reads_to_the_deadline_release_once_and_never_extend_it():
    unreadable = {"venue": "pumpswap", "reason": "vault_unreadable", "slot": 1}
    w, clock, put, woke, lines, pending = _watcher([unreadable] * 40)
    w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="bonding_curve_complete")
    for _ in range(40):
        w.run_once()
        clock.t += 0.4
    assert put == [] and woke == [1] and w.stats["expired"] == 1
    assert w.stats["reads"] <= 16                     # 6 s at 0.4 s: the original deadline, not later


def test_shadow_classifies_a_tagged_unreadable_read_the_same():
    w, clock, put, woke, lines, pending = _watcher(
        [{"venue": "pumpswap", "reason": "pool_or_fee_unreadable", "slot": 1}, _state()], live=False)
    w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="bonding_curve_complete")
    w.run_once()
    clock.t += 0.5
    w.run_once()
    assert w.stats["reads"] == 2 and w.stats["found"] == 1 and put == [] and woke == []


def test_shadow_watches_without_holding_or_handing_back():
    w, clock, put, woke, lines, pending = _watcher([_state()], live=False)
    assert w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="bonding_curve_complete") is False
    assert len(pending) == 0
    w.run_once()
    assert put == [] and woke == [] and w.stats["found"] == 1 and "shadow" in lines[-1]


# --- the lane thread -----------------------------------------------------------------------------

class _Wake:
    def __init__(self, check=None):
        self.n, self.check = 0, check

    def set(self):
        self.n += 1
        if self.check:
            self.check()


def _lane(**kw):
    return L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=lambda: SimpleNamespace(sol_usd=185.0),
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False, now_iso_fn=None,
                      lock_key="k", mode="live", **kw)


def test_the_source_completed_trigger_skips_the_curve_send_only_when_the_watch_takes_it(monkeypatch, tmp_path):
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(L.m, "unresolved_withdraws", lambda conn, network_id: [])
    monkeypatch.setattr(L, "stale_input", lambda snap, now: None)
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: (None, {"mint": MINT, "price_usd": 1.0}))
    sent = []
    monkeypatch.setattr(L.SlotLane, "_traced", lambda self, *a: sent.append(1) or {"status": "filled"})
    done = _slot_buy(FX["completing"]["trade_event_log"])
    lane = _lane(graduation_offer=lambda b, trigger: trigger == "source_completed")
    result = lane.handle(done)
    assert result["reason"] == G.PENDING_REASON and result["graduation_pending"] and sent == []
    lane = _lane(graduation_offer=lambda b, trigger: False)          # shadow, or the watch is full
    assert lane.handle(done)["status"] == "filled" and sent == [1]
    lane = _lane(graduation_offer=lambda b, trigger: pytest.fail("a near-complete curve started a watch"))
    assert lane.handle(_slot_buy(FX["source"]["trade_event_log"]))["status"] == "filled"


def test_a_bonding_curve_revert_is_held_before_the_on_chain_lane_wakes(monkeypatch, tmp_path):
    from sniper.persistence import db_memecoin as m
    held = []
    pending = L.PendingLegs()
    monkeypatch.setattr(L, "GRADUATION_PENDING", pending)
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    monkeypatch.setattr(L.SlotLane, "_reserve_and_execute", lambda self, *a: {
        "status": "failed", "reason": "chain_error:BondingCurveComplete", "pending_id": 7, "sig": "OURS"})
    wake = _Wake(check=lambda: held.append(pending.holds("SIG", time.monotonic())))
    lane = L.SlotLane(conn=c, executor={}, build_fn=None, snapshot_fn=None, armed_ids_fn=None,
                      kill_tripped_fn=None, now_iso_fn=lambda: "2026-10-07T13:34:34+00:00", lock_key="k",
                      mode="live", wake=wake,
                      graduation_offer=lambda b, trigger: pending.add(b, deadline=time.monotonic() + 7))
    snap = SimpleNamespace(observed_mono={"sol_usd": time.monotonic()}, sol_usd=185.0)
    facts = {"their_usd": 900.0, "mint": MINT, "price_usd": 1e-4, "mcap_usd": 1e5}
    lane._queue_timing = {}  # This private-layer test skips handle()'s timing setup.
    result = lane._traced(_slot_buy(FX["source"]["trade_event_log"]), snap, facts, time.perf_counter())
    assert result["graduation"] is True and held == [True]
    c.close()


def test_a_handed_back_buy_skips_the_signal_age_gate_and_releases_its_hold(monkeypatch):
    pending = L.PendingLegs()
    monkeypatch.setattr(L, "GRADUATION_PENDING", pending)
    monkeypatch.setattr(L, "stale_input", lambda snap, now: None)
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: ("mcap $1 over ceiling $0", {}))
    wake = _Wake()
    lane = _lane(wake=wake)
    old = _slot_buy(FX["source"]["trade_event_log"], recv_monotonic=time.monotonic() - 5.0)
    grad = dataclasses.replace(old, graduation=_grad(_state()))
    assert lane.handle(grad)["reason"] == G.AGED_OUT_REASON          # no hold: already had its fallback
    pending.add(grad, deadline=time.monotonic() + 3)
    result = lane.handle(grad)
    assert result["reason"].startswith("mcap $")                        # past the age gate, into admission
    assert len(pending) == 0 and pending.release_kind("SIG", time.monotonic()) == "policy" and wake.n == 1
    assert lane.handle(old)["reason"] == "slot_lane_signal_too_old"     # an ordinary old buy still refuses


def test_the_on_chain_lane_defers_while_held_and_falls_back_once_after(monkeypatch, tmp_path):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_onchain_lane as O
    c = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(c)
    pending = L.PendingLegs()
    monkeypatch.setattr(L, "GRADUATION_PENDING", pending)
    detected = "2026-10-07T13:34:33+00:00"
    assert O.slot_lane_verdict(c, "SIGG", detected) == (None, None)
    pending.add(SimpleNamespace(signature="SIGG"), deadline=time.monotonic() + 30)
    assert O.slot_lane_verdict(c, "SIGG", detected)[0] == "in_flight"
    pending.claim("SIGG", how="expired")
    assert O.slot_lane_verdict(c, "SIGG", detected) == ("graduated", "graduation_released")
    pending.add(SimpleNamespace(signature="SIGP"), deadline=time.monotonic() + 30)
    pending.claim("SIGP", how="policy")
    assert O.slot_lane_verdict(c, "SIGP", detected) == ("graduated", None)
    c.close()


# --- end to end on a real ledger (Gamma, required before live) -----------------------------------

def test_a_reverted_curve_send_requeues_and_reserves_again_on_the_same_origin(monkeypatch, tmp_path, hard_cap):
    """The curve send reserves, reverts BondingCurveComplete and releases; the watcher's hand-back of
    the SAME source buy then runs the lane's real live path (admission on the curve trade,
    reserve_entry, the funded contract) to a fresh 'reserved' row on origin slotlane:<sig>, and the
    PumpSwap build reaches the executor. A signature or origin dedupe anywhere on that path would
    silently kill the main trigger."""
    from sniper.execution import exec_lock
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_source_initiation as initiation
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    pending = L.PendingLegs()
    monkeypatch.setattr(L, "GRADUATION_PENDING", pending)
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    now = time.monotonic()
    sol_usd = 185.0
    snap = L.PrefetchSnapshot(sol_usd=sol_usd, usdc_balance=400.0, sol_balance=0.5, blockhash=BLOCKHASH,
                              last_valid_height=1, observed_mono={"sol_usd": now - 1, "balances": now - 1,
                                                                  "blockhash": now - 1})
    amm = _builder(time.monotonic)
    curve_built = []

    def curve_build(buy, clip_usd, snapshot, **kw):
        curve_built.append(clip_usd)
        return object(), {"target_mint": MINT, "blockhash": BLOCKHASH, "last_valid_height": 1}

    def amm_build(buy, clip_usd, snapshot, **kw):
        amm._leg = dict(amm._leg, usdc_atomic=pcb.clip_atomic(clip_usd))     # the leg the prefetch keeps
        return ps.SlotLanePumpSwapBuilder.build(amm, buy, clip_usd, snapshot, **kw)
    sends = []

    def execute(conn_, pending_row, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        # PR #486 (2026-10-20): automatic Solana entries carry no source-slot window; both bounds are None.
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        row = dict(conn_.execute("SELECT status, origin_event_id, mint FROM memecoin_pending_entries WHERE id=?",
                                 (pending_row["id"],)).fetchone())
        sends.append((row, validity.get("route_target")))
        if len(sends) == 1:
            m.release_pending(conn_, pending_row["id"], "chain_error:BondingCurveComplete")
            return {"status": "failed", "reason": "chain_error:BondingCurveComplete", "sig": "OURS"}
        return {"status": "failed", "reason": "chain_error:test_stops_here", "sig": "OURS2"}
    taken = []
    lane = L.SlotLane(conn=c, executor={"slot_execute_fn": execute},
                      build_fn=L.dispatch_build({"pump_curve": SimpleNamespace(build=curve_build),
                                                 "pump_amm": SimpleNamespace(build=amm_build)}),
                      snapshot_fn=lambda: snap, armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                      now_iso_fn=lambda: "2026-10-07T13:34:34+00:00", lock_key="sol-test", mode="live",
                      shadow_clip_fn=lambda: 50.0,
                      graduation_offer=lambda b, trigger: taken.append((b, trigger))
                      or pending.add(b, deadline=time.monotonic() + 7))
    buy = _slot_buy(FX["source"]["trade_event_log"], recv_monotonic=now, token_program=pcb.TOKEN_2022_PROGRAM)
    first = lane.handle(buy)
    assert first["status"] == "failed" and first["graduation"] is True, first
    assert taken and taken[0][1] == "bonding_curve_complete" and pending.holds("SIG", time.monotonic())
    state, _ = _at_premium(_state(), 0.10, sol_usd)
    back = dataclasses.replace(taken[0][0], graduation=_grad(state))
    second = lane.handle(back)
    assert len(sends) == 2, second
    (row1, _), (row2, route2) = sends
    assert row1 == {"status": "reserved", "origin_event_id": "slotlane:SIG", "mint": MINT}
    assert row2 == {"status": "reserved", "origin_event_id": "slotlane:SIG", "mint": MINT}
    assert route2.endswith(POOL) and len(curve_built) == 1
    attempts = c.execute("SELECT origin_event_id, terminal_reason FROM memecoin_entry_attempts "
                         "ORDER BY decision_started_at, rowid").fetchall()
    assert [tuple(a) for a in attempts] == [("slotlane:SIG", "chain_error:BondingCurveComplete"),
                                            ("slotlane:SIG", "chain_error:test_stops_here")]
    assert len(pending) == 0 and lane.graduation_stats["sent"] == 1
    c.close()


def test_the_heartbeat_names_the_graduation_counters(tmp_path):
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), slot_lane_status={
        "mode": "live", "signal": {},
        "graduation": {"offered": 2, "busy": 0, "requeued": 1, "found": 0, "expired": 1, "refused": 0,
                       "reads": 9, "read_errors": 1, "notified": 1, "last": "expired", "junk": 5},
        "graduation_lane": {"offered_presend": 0, "offered_revert": 2, "reentered": 1, "aged_out": 0,
                            "sent": 1}})
    got = json.loads(path.read_text())["slot_lane"]["graduation"]
    assert got == {"offered": 2, "busy": 0, "requeued": 1, "found": 0, "expired": 1, "refused": 0,
                   "reads": 9, "read_errors": 1, "notified": 1, "last": "expired",
                   "lane_offered_presend": 0, "lane_offered_revert": 2, "lane_reentered": 1,
                   "lane_aged_out": 0, "lane_sent": 1}


# --- SOL-3 (Codex two-day review 2026-10-07): the deadline is checked around every read ----------

def _stalling_watcher(stall_s):
    """A watcher whose first read takes `stall_s` on the fake clock, then answers a valid pool."""
    w, clock, put, woke, lines, pending = _watcher([])
    reads = []

    def read(mint):
        reads.append(mint)
        if len(reads) == 1:
            clock.t += stall_s
        return _state()
    w.read_fn = read
    return w, clock, put, woke, lines, pending, reads


def test_a_read_that_stalls_past_the_deadline_requeues_nothing_and_the_next_job_reads_nothing():
    w, clock, put, woke, lines, pending, reads = _stalling_watcher(10.0)
    line = FX["source"]["trade_event_log"]
    assert w.offer(_slot_buy(line, mint=MINT), trigger="bonding_curve_complete")
    assert w.offer(_slot_buy(line, mint=MINT, signature="SIG2"), trigger="bonding_curve_complete")
    w.run_once()
    assert put == [] and len(reads) == 1                          # the second job never read
    assert w.stats["requeued"] == 0 and w.stats["expired"] == 2 and w.stats["found"] == 0
    assert any("-> expired_after_read reads=1" in x for x in lines)
    assert any("-> expired reads=0" in x for x in lines)
    assert not pending.holds("SIG", clock.t) and not pending.holds("SIG2", clock.t) and woke


def test_a_read_that_lands_inside_the_deadline_still_requeues_once():
    w, clock, put, woke, lines, pending, reads = _stalling_watcher(5.0)
    assert w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="bonding_curve_complete")
    w.run_once()
    assert len(put) == 1 and w.stats["requeued"] == 1 and w.stats["expired"] == 0


def test_a_migration_notice_cannot_extend_the_watch():
    w, clock, put, woke, lines, pending, reads = _stalling_watcher(0.0)
    assert w.offer(_slot_buy(FX["source"]["trade_event_log"], mint=MINT), trigger="bonding_curve_complete")
    clock.t += 6.1
    w.notify(POOL)
    w.run_once()
    assert put == [] and reads == [] and w.stats["expired"] == 1

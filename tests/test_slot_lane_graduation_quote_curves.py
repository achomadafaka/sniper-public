"""GRADUATION DIRECT FOR QUOTE-TOKEN CURVES (operator "fix" via Alpha, 2026-10-07, HI EtVFMDoB 22:34Z).

ResellCalendar bought HI through Jupiter: USDC 800 -> WLDTE73P -> a pump curve quoted in WLDTE73P, and its
own TradeEvent left real_token_reserves 0. The lane spent a 142 ms leg, then its build refused 'curve is
complete'; graduation direct never fired (SOL curves only), and the on-chain lane refused
missing_receipt_proof because the watcher's route valuation saw TWO rising mints (the coin and a 7.04
WLDTE73P residue). Pump migrated HI into PumpSwap coin/WLDTE73P FAtxUWPp; no coin/wSOL pool exists.
Real data: tests/fixtures/pump_graduation_quote_hi.json (research key, flat book). Every part is behind
MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES=live; a wSOL curve is the same with it on or off."""
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
from sniper.services import memecoin_source_initiation as SI
from sniper.services import memecoin_source_valuation as V

HERE = Path(__file__).parent / "fixtures"
HI = json.loads((HERE / "pump_graduation_quote_hi.json").read_text())
BL = json.loads((HERE / "pump_graduation_blastar.json").read_text())
MINT, QUOTE, POOL = HI["mint"], HI["quote_mint"], HI["pool"]
USER = "4E1f85HhyYQ8LwWjxsGd7JuemNkBC8hjkDN3VHo98Q3o"
BLOCKHASH = "11111111111111111111111111111111"


def _buy(line, **over):
    hit = S._curve(anchor_event_bytes(line), b"", any_user=True)
    fields = dict(source_user_id="u1", handle="ResellCalendar", owner=HI["owner"], signature="SIGQ",
                  slot=HI["slot"], token_program=None, trade_event_log=line,
                  recv_monotonic=time.monotonic(), recv_utc="2026-10-07T22:34:25.4+00:00", **hit)
    fields.update(over)
    return S.SlotBuy(**fields)


def _replay(calls):
    calls = list(calls)

    def rpc(method, params):
        call = calls.pop(0)
        assert call["method"] == method and call["params"][0] == params[0]
        return call["result"]
    return rpc


def _hi_state():
    return read_native_state(MINT, _replay(HI["native_state_calls"]), quote_mint=QUOTE)


@pytest.fixture
def quote_live(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", "live", raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_curve", "pump_amm"}),
                        raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")
    monkeypatch.setattr(L, "stale_input", lambda snap, now: None)


def _lane(**kw):
    return L.SlotLane(conn=None, executor={}, build_fn=None,
                      snapshot_fn=lambda: SimpleNamespace(sol_usd=150.0),
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False, now_iso_fn=None,
                      lock_key="k", mode="live", **kw)


# --- the wSOL path does not move -----------------------------------------------------------------

def _sol_run(monkeypatch, knob):
    """Everything a wSOL graduation touches: the pool read, the checks, the build bytes, the trigger."""
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", knob, raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})
    state = read_native_state(BL["mint"], _replay(BL["native_state_calls"]))
    clock = lambda: 100.0
    b = ps.SlotLanePumpSwapBuilder(user=USER, jup_call=lambda *a, **k: None, rpc=lambda *a: None, clock=clock)
    b._buyback = ps.buyback_fee_recipient_from_global_config(BL["global_config_b64"])
    b._protocol_recipient = ps.protocol_fee_recipient_from_global_config(BL["global_config_b64"])
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": pcb.clip_atomic(50.0),
              "min_out_lamports": 270_000_000, "out_lamports": 272_000_000, "fetched_at": 100.0}
    src = pcb.curve_source_price_usd(pcb.parse_trade_event(BL["source"]["trade_event_log"]), 185.0)
    quote = int(src * 1.1 / 185.0 * state["base_reserve"] / 1e6 * 1e9) - state["virtual_quote"]
    grad = G.payload(dict(state, quote_reserve=quote), trigger="bonding_curve_complete",
                     offered_mono=0.0, read_ms=1.0, now_mono=1.0)
    source = _buy(BL["source"]["trade_event_log"], owner="OWNER", signature="SIG", mint=BL["mint"])
    msg, _detail = b.build(dataclasses.replace(source, graduation=grad), 50.0,
                           SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=185.0))
    offers = []
    monkeypatch.setattr(L, "admission_refusal", lambda bb, **k: (None, {"mint": BL["mint"], "price_usd": 1.0}))
    monkeypatch.setattr(L, "stale_input", lambda snap, now: None)
    lane = _lane(graduation_offer=lambda bb, **k: offers.append(k) or True)
    done = _buy(BL["completing"]["trade_event_log"], owner="OWNER", signature="SIG")
    result = lane.handle(done)
    reads, put = [], []

    def read(*args, **kw):                 # the watcher's read call, exactly as made
        reads.append((args, kw))
        return state
    w = G.GraduationWatcher(read_fn=read, requeue_fn=put.append, wake_fn=lambda: None,
                            pending=L.PendingLegs(), live=True, log=lambda line: None,
                            clock=SimpleNamespace(monotonic=lambda: 5.0), deadline_s=6.0, poll_s=0.4)
    w.offer(done, trigger="source_completed")
    w.run_once()
    return (state["account"], G.canonical_pool(BL["mint"]), G.pool_refusal(state, BL["mint"]),
            G.curve_completed(done), G.quote_curve_completed(done), bytes(msg), result, offers,
            lane.graduation_stats, reads, put[0].graduation["pool"])


def test_sol_graduation_is_byte_identical_with_the_quote_knob_on_and_off(monkeypatch):
    off = _sol_run(monkeypatch, "off")
    on = _sol_run(monkeypatch, "live")
    assert off == on
    assert off[0] == BL["pool"] and off[2] is None and off[3] is True and off[4] is None
    assert off[6]["reason"] == G.PENDING_REASON and off[7] == [{"trigger": "source_completed"}]
    assert off[9] == [((BL["mint"],), {})] and off[10] == BL["pool"]     # read_fn(mint), nothing more


# --- the trigger, before any leg ------------------------------------------------------------------

def test_only_the_quote_curve_the_source_completed_names_its_quote():
    hi = _buy(HI["trade_event_log"])
    assert hi.virtual_sol_reserves == 0 and hi.real_token_reserves == 0
    assert G.quote_curve_completed(hi) == QUOTE and not G.curve_completed(hi)
    assert G.quote_curve_completed(dataclasses.replace(hi, mint=BL["mint"])) is None     # another coin
    assert G.quote_curve_completed(dataclasses.replace(hi, venue="pump_amm")) is None
    assert G.quote_curve_completed(_buy(BL["completing"]["trade_event_log"])) is None    # a wSOL curve


def test_a_completed_quote_curve_goes_to_the_pool_watch_before_any_leg(quote_live, monkeypatch):
    offers = []
    lane = _lane(graduation_offer=lambda b, **k: offers.append((b, k)) or True,
                 exotic_offer=lambda b, **k: pytest.fail("a leg was spent on a completed curve"),
                 pending_legs=L.PendingLegs())
    result = lane.handle(_buy(HI["trade_event_log"]))
    assert result["reason"] == G.PENDING_REASON and result["graduation_pending"] and result["quote_mint"] == QUOTE
    (offered, kw), = offers
    assert kw == {"trigger": "source_completed", "quote_mint": QUOTE} and offered.ondemand is None
    assert lane.graduation_stats["offered_quote"] == 1
    # Knob off: the exotic path as before (the leg is asked, nothing is watched).
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", "off")
    legs = []
    lane = _lane(graduation_offer=lambda b, **k: pytest.fail("watched with the knob off"),
                 exotic_offer=lambda b, **k: legs.append(b) or True, pending_legs=L.PendingLegs())
    assert lane.handle(_buy(HI["trade_event_log"]))["reason"] == L.EXOTIC_PENDING_REASON and len(legs) == 1


# --- the pool -------------------------------------------------------------------------------------

def test_the_watch_reads_the_coin_quote_pool_and_accepts_only_it():
    state = _hi_state()
    assert state["account"] == POOL == G.canonical_pool(MINT, QUOTE) != G.canonical_pool(MINT)
    assert state["quote_decimals"] == 8 and state["quote_token_program"] == pcb.TOKEN_PROGRAM
    assert G.pool_refusal(state, MINT, QUOTE) is None
    assert G.pool_refusal(state, MINT) == "not_canonical_pool"
    facts = state["pool_facts"]
    assert G.pool_refusal(dict(state, pool_facts=dict(facts, quote_mint=pcb.WSOL_MINT)), MINT, QUOTE) == "pool_mint_mismatch"
    assert G.pool_refusal(dict(state, pool_facts=dict(facts, pool_quote_token_account=USER)), MINT, QUOTE) == "vault_mismatch"
    clock, put, reads = SimpleNamespace(monotonic=lambda: 10.0), [], []

    def read(mint, **kw):
        reads.append(kw)
        return state
    w = G.GraduationWatcher(read_fn=read, requeue_fn=put.append, wake_fn=lambda: None,
                            pending=L.PendingLegs(), live=True, log=lambda line: reads.append(line),
                            clock=clock, deadline_s=6.0, poll_s=0.4)
    assert w.offer(_buy(HI["trade_event_log"]), trigger="source_completed", quote_mint=QUOTE)
    w.run_once()
    assert reads[0] == {"quote_mint": QUOTE} and "quote=WLDTE73P" in reads[-1]
    assert put[0].graduation["pool"] == POOL and put[0].graduation["pool_facts"]["quote_decimals"] == 8


# --- the hand-back: a fresh leg, held before the pool hold goes ----------------------------------

def test_a_quote_hand_back_holds_its_leg_before_releasing_the_pool_then_takes_the_requeued_path(
        quote_live, monkeypatch):
    grad_hold, legs = L.PendingLegs(), L.PendingLegs()
    monkeypatch.setattr(L, "GRADUATION_PENDING", grad_hold)
    seen = []
    wake = SimpleNamespace(set=lambda: seen.append((legs.holds("SIGQ", time.monotonic()),
                                                    grad_hold.holds("SIGQ", time.monotonic()))))
    lane = _lane(exotic_offer=lambda b, **k: True, pending_legs=legs, wake=wake)
    grad = G.payload(_hi_state(), trigger="source_completed", offered_mono=0.0, read_ms=1.0, now_mono=1.0)
    back = _buy(HI["trade_event_log"], recv_monotonic=time.monotonic() - 4.0, graduation=grad)
    grad_hold.add(back, deadline=time.monotonic() + 3)
    result = lane.handle(back)
    assert result["reason"] == L.EXOTIC_PENDING_REASON
    assert seen == [(True, False)]                     # the leg held before the pool hold was released
    assert grad_hold.release_kind("SIGQ", time.monotonic()) == "claimed"
    # The worker's requeue: graduation AND a fresh leg. It runs the requeued path, not 'aged out'.
    monkeypatch.setattr(L.SlotLane, "_handle", lambda self, b, hand_off=True: {"status": "filled"})
    requeued = dataclasses.replace(back, ondemand={"leg": {}, "quote": (8, 1.0, 0, 0), "quote_mint": QUOTE,
                                                   "requeued_mono": time.monotonic()})
    assert lane.handle(requeued)["status"] == "filled"
    assert lane.exotic_stats["reentered"] == 1 and lane.graduation_stats["aged_out"] == 0


# --- the build: the pool and the leg price our clip, the source's curve trade prices the caps ----

def test_a_quote_graduation_builds_on_the_coin_quote_pool_under_the_curve_caps(quote_live, monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})
    state = _hi_state()
    curve = pcb.parse_trade_event(HI["trade_event_log"])
    quote_usd = 0.05
    src, average = pcb.quote_curve_prices(curve, 8, quote_usd, 6)

    def at(premium):
        q = int(src * (1 + premium) / quote_usd * state["base_reserve"] / 1e6 * 1e8) - state["virtual_quote"]
        return G.payload(dict(state, quote_reserve=q), trigger="source_completed", offered_mono=0.0,
                         read_ms=1.0, now_mono=1.0)
    b = ps.SlotLanePumpSwapBuilder(user=USER, jup_call=lambda *a, **k: None, rpc=lambda *a: None,
                                   clock=lambda: 50.0)
    b._buyback = ps.buyback_fee_recipient_from_global_config(BL["global_config_b64"])
    b._protocol_recipient = ps.protocol_fee_recipient_from_global_config(BL["global_config_b64"])
    spend_q = int(10.0 / quote_usd * 1e8)
    leg = {"ixs": [], "tables": [], "usdc_atomic": 10_000_000, "min_out_lamports": spend_q,
           "out_lamports": spend_q + 50, "fetched_at": 50.0, "quote": {}, "output_mint": QUOTE}
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)
    buy = _buy(HI["trade_event_log"], graduation=at(0.10))
    msg, detail = b.build(buy, 10.0, snap, leg=leg, quote=(8, quote_usd, 0, 0))
    assert msg is not None, detail
    assert detail["target_mint"] == MINT and detail["route_target"].endswith(POOL)
    assert b.last_build["source_average_price_usd"] == pytest.approx(average)
    fee_ata = str(ps.ata(ps._pk(b._protocol_recipient), ps._pk(QUOTE), ps._pk(pcb.TOKEN_PROGRAM)))
    wsol_ata = str(ps.ata(ps._pk(b._protocol_recipient), ps._pk(pcb.WSOL_MINT), ps._pk(pcb.TOKEN_PROGRAM)))
    keys = [str(k) for k in msg.account_keys]
    assert fee_ata in keys and wsol_ata not in keys
    # The cap is the CURVE's: a pool already 40% over the source refuses before signing.
    msg, why = b.build(dataclasses.replace(buy, graduation=at(0.40)), 10.0, snap, leg=leg,
                       quote=(8, quote_usd, 0, 0))
    assert msg is None and "source-price cap refuses" in why


def test_a_curve_complete_build_refusal_offers_the_quote_watch_without_the_leg(quote_live, monkeypatch, tmp_path):
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    monkeypatch.setattr(L.SlotLane, "_reserve_and_execute", lambda self, *a: {
        "status": "aborted", "reason": "slot lane build refused: curve is complete: no real token reserves left",
        "pending_id": 7})
    offers = []
    lane = L.SlotLane(conn=c, executor={}, build_fn=None, snapshot_fn=None, armed_ids_fn=None,
                      kill_tripped_fn=None, now_iso_fn=lambda: "2026-10-07T22:34:25+00:00", lock_key="k",
                      mode="live", graduation_offer=lambda b, **k: offers.append((b, k)) or True)
    snap = SimpleNamespace(observed_mono={"sol_usd": time.monotonic()}, sol_usd=150.0)
    facts = {"their_usd": 800.0, "mint": MINT, "price_usd": 1e-5, "mcap_usd": 1e4}
    buy = _buy(HI["trade_event_log"], quote_mint=QUOTE, ondemand={"leg": {}, "quote": (8, 1.0, 0, 0)})
    lane._queue_timing = {}  # This private-layer test skips handle()'s timing setup.
    result = lane._traced(buy, snap, facts, time.perf_counter())
    (offered, kw), = offers
    assert result["graduation"] is True and kw == {"trigger": "build_refused_complete", "quote_mint": QUOTE}
    assert offered.ondemand is None                    # the hand-back quotes a fresh leg
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", "off")
    result = lane._traced(buy, snap, facts, time.perf_counter())
    assert "graduation" not in result and len(offers) == 1
    c.close()


# --- the on-chain lane's receipt: the coin is the target, the quote's residue a hop ---------------

def test_the_receipt_binds_the_coin_and_prices_only_the_quote_that_went_into_it(monkeypatch):
    tx, owner = HI["source_tx"], HI["owner"]
    before = json.dumps(tx, sort_keys=True)
    sol_usd = (150.0, 1000.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", "off", raising=False)
    off = V.value_source_buy(tx, owner, sol_usd=sol_usd, now=1000.0)
    assert off["unknown_reason"] == "route_target_ambiguous" and SI.solana_receipt_initiation(tx, owner, off) is None
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", "live")
    on = V.value_source_buy(tx, owner, sol_usd=sol_usd, now=1000.0)
    assert on["basis"] == "route_owner_quote_out" and on["mint"] == MINT, on
    assert on["token_amount_atomic"] == "17575021990611"
    hop = on["route_intermediate"]
    bought, residue = int(hop["inflow_atomic"]), int(hop["residue_atomic"])
    assert hop["mint"] == QUOTE and residue == 7_042_335_461 and bought > residue
    usdc = int(on["quote_out_atomic"][V.USDC])
    assert usdc == 800_000_000 * (bought - residue) // bought < 800_000_000
    assert on["source_size_usd"] == pytest.approx(usdc / 1e6)
    proof = SI.solana_receipt_initiation(tx, owner, on)
    assert proof is not None and proof["mint"] == MINT and int(proof["payment_atomic"]) == usdc
    assert int(proof.get("route_payment_atomic") or 0) <= usdc
    assert json.dumps(tx, sort_keys=True) == before        # valuation never writes into the receipt


# --- guards (Gamma's review of 97af16c84: each survived deletion) ---------------------------------

def _with_owner_quote_delta(extra):
    """The HI receipt with the owner's post balance of the curve's quote raised by `extra` atoms: the
    balance then moves by more than its transfers (a fee-on-transfer token, or quote the owner held
    before), so the residue no longer says what share of the bought quote went into the coin."""
    import copy
    tx = copy.deepcopy(HI["source_tx"])
    for bal in tx["meta"]["postTokenBalances"]:
        if bal.get("owner") == HI["owner"] and bal.get("mint") == QUOTE:
            ui = bal["uiTokenAmount"]
            ui["amount"] = str(int(ui["amount"]) + extra)
            return tx
    raise AssertionError("no owner quote balance in the HI receipt")


@pytest.mark.parametrize("extra", [1, 5_000_000_000])
def test_a_quote_balance_that_disagrees_with_its_transfers_is_never_pro_rated(monkeypatch, extra):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES", "live", raising=False)
    tx = _with_owner_quote_delta(extra)
    facts = V.value_source_buy(tx, HI["owner"], sol_usd=(150.0, 1000.0), now=1000.0)
    assert facts["unknown_reason"] == "route_intermediate_unbound" and facts["basis"] == "unknown"
    assert facts["source_size_usd"] is None and "route_intermediate" not in facts
    assert SI.solana_receipt_initiation(tx, HI["owner"], facts) is None


def test_a_coin_quote_pool_whose_quote_vault_is_token_2022_is_refused():
    state = _hi_state()
    assert G.pool_refusal(state, MINT, QUOTE) is None
    assert G.pool_refusal(dict(state, quote_token_program=pcb.TOKEN_2022_PROGRAM), MINT, QUOTE) == \
        "token_program_unsupported"


def test_a_graduation_pool_quoted_in_another_token_than_the_curve_refuses(quote_live, monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    other = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"          # WBTC (Portal), not WLDTE73P
    state = _hi_state()
    grad = G.payload(dict(state, pool_facts=dict(state["pool_facts"], quote_mint=other)),
                     trigger="source_completed", offered_mono=0.0, read_ms=1.0, now_mono=1.0)
    b = ps.SlotLanePumpSwapBuilder(user=USER, jup_call=lambda *a, **k: None, rpc=lambda *a: None,
                                   clock=lambda: 50.0)
    b._buyback = ps.buyback_fee_recipient_from_global_config(BL["global_config_b64"])
    b._protocol_recipient = ps.protocol_fee_recipient_from_global_config(BL["global_config_b64"])
    leg = {"ixs": [], "tables": [], "usdc_atomic": 10_000_000, "min_out_lamports": 20_000_000_000,
           "out_lamports": 20_000_000_050, "fetched_at": 50.0, "quote": {}, "output_mint": other}
    msg, why = b.build(_buy(HI["trade_event_log"], graduation=grad), 10.0,
                       SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0),
                       leg=leg, quote=(8, 0.05, 0, 0))
    assert msg is None and why == "graduation_quote_mismatch"

"""ONE-SHOT RE-QUOTE OF AN EXOTIC LEG THAT DOES NOT FIT THE PACKET (Alpha GO 2026-10-07).

3fe2e25ea2aa (ResellCalendar 5z3wbw3X, 21:26Z, exotic pump_amm, two-hop Jupiter leg) was refused at
1,293 bytes before any tip: a lost fill. Most of those bytes are the route's own accounts, so no
lookup table fixes them; a tighter route does. With MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE=oversize a build
refused 'over the packet' releases its reservation and goes BACK TO THE ON-DEMAND WORKER for a leg on
maxAccounts 20, then onlyDirectRoutes, each at most once. The worker re-prices and re-admits it at its
own quote and hands it back like any requeued leg, so the lane thread and the wallet lock never wait
(Alpha: holding them would cost OTHER buys). Its hold is the leg's, moved out in place."""
import contextlib
import dataclasses
import sqlite3
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_onchain_lane as O
from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_slot_signal as S

OVER = "transaction is 1293 bytes, over the 1232-byte packet"


class _Trace:
    def __init__(self):
        self.marks = []

    def mark_substage(self, name, **kw):
        self.marks.append((name, kw))

    def get(self, name):
        return next((kw for n, kw in self.marks if n == name), None)


def _buy(**over):
    fields = dict(venue="pump_amm", source_user_id="u1", handle="ResellCalendar", owner="OWNER",
                  signature="SIGR", slot=1, mint="MINT", pool="POOL", sol_amount=0, token_amount=1,
                  virtual_sol_reserves=0, virtual_token_reserves=0, real_sol_reserves=0,
                  real_token_reserves=0, token_program=None, trade_event_log="",
                  recv_monotonic=time.monotonic(), recv_utc="t", quote_mint="QUOTE",
                  ondemand={"leg": {"hops": 2}, "quote": (8, 0.4, 0, 0), "quote_mint": "QUOTE"})
    fields.update(over)
    return S.SlotBuy(**fields)


@pytest.fixture
def oversize(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE", "oversize", raising=False)
    released = []
    monkeypatch.setattr(L.m, "release_pending", lambda conn, pid, reason: released.append((pid, reason)))
    return released


def _lane(offers, *, take=True, legs=None):
    legs = legs if legs is not None else L.PendingLegs()

    def offer(buy, **kw):
        offers.append((buy, kw))
        return take
    return L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=None, armed_ids_fn=None,
                      kill_tripped_fn=None, now_iso_fn=None, lock_key="k", mode="live",
                      exotic_offer=offer, pending_legs=legs), legs


def test_off_refuses_as_before_and_offers_nothing(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE", "off", raising=False)
    offers = []
    lane, legs = _lane(offers)
    legs.add(_buy(), deadline=time.monotonic() + 1)
    assert lane._requote_over_packet(_buy(), 100.0, OVER, 7, _Trace()) is None and offers == []


def test_an_over_packet_build_goes_back_to_the_worker_and_the_lane_returns_at_once(oversize):
    offers = []
    lane, legs = _lane(offers)
    buy = _buy()
    legs.add(buy, deadline=time.monotonic() + 0.1)
    trace = _Trace()
    t0 = time.monotonic()
    result = lane._requote_over_packet(buy, 100.0, OVER, 7, trace)
    assert time.monotonic() - t0 < 0.05                                      # no wait on the lane thread
    assert result == {"status": "rejected", "reason": L.REQUOTE_PENDING_REASON, "exotic_pending": True}
    (offered, kw), = offers
    assert offered is buy and kw == {"clip_usd": 100.0, "quote_params": "&maxAccounts=20"}
    assert legs.holds(buy.signature, now=time.monotonic() + L.REQUOTE_HOLD_S - 0.2)   # moved out, in place
    assert oversize == [(7, "slot_lane_build:requote:" + OVER[:30])]
    rec = trace.get("slot_requote")
    assert rec == {"reason": "over_packet", "attempt": 1, "params": "&maxAccounts=20", "bytes_before": 1293}


def test_the_second_shape_then_never_a_third(oversize):
    offers = []
    lane, legs = _lane(offers)
    once = _buy(ondemand={"leg": {}, "quote": (8, 0.4, 0, 0), "quote_mint": "QUOTE", "requote_attempts": 1})
    legs.add(once, deadline=time.monotonic() + 1)
    assert lane._requote_over_packet(once, 100.0, OVER, 7, _Trace())["exotic_pending"]
    assert offers[-1][1]["quote_params"] == "&onlyDirectRoutes=true"
    twice = dataclasses.replace(once, ondemand=dict(once.ondemand, requote_attempts=2))
    assert lane._requote_over_packet(twice, 100.0, OVER, 8, _Trace()) is None and len(offers) == 1


def test_no_requote_for_another_refusal_an_unheld_buy_or_a_busy_worker(oversize):
    offers = []
    lane, legs = _lane(offers)
    assert lane._requote_over_packet(_buy(), 100.0, OVER, 7, _Trace()) is None      # not held (a resume)
    legs.add(_buy(), deadline=time.monotonic() + 1)
    assert lane._requote_over_packet(_buy(), 100.0, "source-price cap refuses", 7, _Trace()) is None
    assert lane._requote_over_packet(_buy(ondemand=None), 100.0, OVER, 7, _Trace()) is None
    assert offers == [] and oversize == []
    busy, busy_legs = _lane(offers, take=False)
    buy = _buy()
    busy_legs.add(buy, deadline=time.monotonic() + 0.1)
    assert busy._requote_over_packet(buy, 100.0, OVER, 7, _Trace()) is None
    assert not busy_legs.holds(buy.signature, now=time.monotonic() + 0.5) and oversize == []


def test_a_requoted_buy_passes_the_signal_age_gate_an_ordinary_one_does_not(monkeypatch):
    monkeypatch.setattr(L, "stale_input", lambda snap, now: None)
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: ("past the age gate", {}))
    lane = L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=lambda: SimpleNamespace(sol_usd=150.0),
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False, now_iso_fn=None,
                      lock_key="k", mode="live")
    old = time.monotonic() - 1.0
    assert lane._handle(_buy(recv_monotonic=old))["reason"] == "slot_lane_signal_too_old"
    requoted = _buy(recv_monotonic=old, ondemand={"leg": {}, "quote": (8, 0.4, 0, 0), "quote_mint": "QUOTE",
                                                  "requote_attempts": 1})
    assert lane._handle(requoted)["reason"] == "past the age gate"


def _verdict_conn(reason, pending=None):
    from test_memecoin_onchain_lane import ARMED_UID, NOW, conn_with
    conn = conn_with([("SIGV", ARMED_UID, 5)])
    started = NOW.replace(second=0).isoformat()
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "decision_started_at, terminal_status, terminal_reason) VALUES (?,?,?,?,?,?)",
                 ("slot-SIGV", "slotlane:SIGV", "slot_lane", started, "rejected", L.REQUOTE_PENDING_REASON))
    if pending is not None:
        conn.execute("INSERT INTO memecoin_pending_entries (attempt_id, status, resolution_reason, tx_sig)"
                     " VALUES (?,?,?,?)", ("slot-SIGV", pending, reason, None))
    return conn, NOW.isoformat()


def test_the_on_chain_lane_defers_to_a_requote_hold_after_the_attempt_row(oversize, monkeypatch):
    legs = L.PendingLegs()
    monkeypatch.setattr(L, "EXOTIC_PENDING", legs)
    conn, detected = _verdict_conn("slot_lane_build:requote:" + OVER[:30], pending="failed")
    legs.add(SimpleNamespace(signature="SIGV"), deadline=time.monotonic() + 1)
    assert O.slot_lane_verdict(conn, "SIGV", detected) == ("in_flight", None)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE", "off")
    assert O.slot_lane_verdict(conn, "SIGV", detected)[0] != "in_flight"          # knob off: as before


def test_a_released_requote_keeps_the_build_refused_fallback_class(oversize, monkeypatch):
    """A re-quote that ends with no newer attempt row (the worker refused it, its hold ran out) must
    leave the buy today's ONE fallback: build_refused, not a classless failure."""
    monkeypatch.setattr(L, "EXOTIC_PENDING", L.PendingLegs())
    conn, detected = _verdict_conn("slot_lane_build:requote:" + OVER[:30], pending="failed")
    assert O.slot_lane_verdict(conn, "SIGV", detected) == ("failed", "build_refused")


# --- the worker: the tighter leg, priced and re-admitted at its own quote -----------------------------

def test_the_worker_requotes_on_the_given_shape_without_a_warm_leg_and_counts_it(monkeypatch):
    from sniper.execution import pump_curve_buy as P
    from sniper.execution import raydium_launchlab_buy as R
    seen = {}

    def prefetch(usdc_atomic, slippage_bps, *, quote_params, output_mint, jup_call, **kw):
        seen.update(params=quote_params, output=output_mint)
        return {"ixs": [], "tables": [], "usdc_atomic": usdc_atomic, "out_lamports": 25_000_000_000,
                "min_out_lamports": 24_500_000_000, "fetched_at": 0.0, "output_mint": output_mint,
                "quote": {"priceImpactPct": "0.01"}}
    monkeypatch.setattr(R, "prefetch_usdc_sol_leg", prefetch, raising=False)
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", prefetch)
    builder = SimpleNamespace(user="USER", _alt_cache={}, clock=time.monotonic, REASON_PREFIX="slot_lane_pump",
                              read_rpc=None, pools=SimpleNamespace(rpc=None, get=lambda pool: {}),
                              ondemand_quote=lambda buy: ("QUOTE", (8, 0, 0), None),
                              ondemand_facts=lambda buy: {})
    monkeypatch.setattr(R.OnDemandLegWorker, "_live_quote", lambda self, b, m, q, facts=None: ((8, 0, 0), None))
    admitted, requeued = [], []
    warm = SimpleNamespace(get=lambda *a: pytest.fail("a re-quote used a warm leg"), note=lambda *a, **k: None)
    monkeypatch.setattr(R, "warm_legs_on", lambda: True)
    w = R.OnDemandLegWorker(builders={"pump_amm": builder}, admission_fn=lambda b, q: admitted.append(q),
                            snapshot_fn=None, clip_fn=lambda: 100.0, offer_fn=lambda *a: None, start=False,
                            live_fn=lambda venue: True, requeue_fn=requeued.append, warm=warm)
    first = _buy(ondemand={"leg": {}, "quote": (8, 0.4, 0, 0), "quote_mint": "QUOTE", "resized": True})
    result = w.process(first, clip_usd=100.0, quote_params="&maxAccounts=20")
    assert result["status"] == "requeued", result
    assert seen == {"params": "&maxAccounts=20", "output": "QUOTE"}
    od = requeued[0].ondemand
    paid = 100.0 / (25_000_000_000 / 1e8)
    assert od["requote_attempts"] == 1 and od["requoted"] == "&maxAccounts=20" and od["resized"] is True
    assert od["quote"][1] == pytest.approx(paid * 0.99) and admitted == [(8, pytest.approx(paid * 0.99))]
    assert "requote_offered_mono" in od
    w.process(requeued[0], clip_usd=100.0, quote_params="&onlyDirectRoutes=true")
    assert requeued[1].ondemand["requote_attempts"] == 2
    # Gamma LOW on 1ee444601: an UNRESIZED buy stays unresized through its re-quote, so its one
    # legitimate resize (a wallet-capped clip, SCALED_CLIP_REASON) is still allowed afterwards.
    plain = _buy(ondemand={"leg": {}, "quote": (8, 0.4, 0, 0), "quote_mint": "QUOTE"})
    w.process(plain, clip_usd=100.0, quote_params="&maxAccounts=20")
    assert requeued[2].ondemand["resized"] is False


# --- end to end on a real ledger: over the packet, back to the worker, the tighter leg fills ----------

from test_slot_lane_quote_curve import (BLOCKHASH, EXACT_IN, WBTC, _builder as _curve_builder,  # noqa: E402
                                        _leg as _curve_leg, _slot_buy as _curve_buy, curve_listed)  # noqa: F401


def test_an_over_packet_build_requotes_through_the_worker_and_the_tighter_leg_fills(
        curve_listed, monkeypatch, tmp_path):
    import json
    from sniper.execution import exec_lock
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_source_initiation as SI
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_REQUOTE", "oversize", raising=False)  # real ledger writes
    monkeypatch.setattr(SI, "source_initiation_refusal", lambda *_: None)
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {}, raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 0.0)
    monkeypatch.setattr("sniper.services.memecoin_wallet_caps.entry_cap_usd", lambda *a, **k: None)
    b = _curve_builder()
    assert b.ondemand_quote(_curve_buy(EXACT_IN))[2] is None      # the worker's mint read, same builder
    b.clock = lambda: 50.0
    now = time.monotonic()
    usd = 10.0 / (11_809 / 1e8)
    buy = dataclasses.replace(_curve_buy(EXACT_IN), recv_monotonic=now, ondemand={
        "leg": _curve_leg(spend_q=11_809, usdc=10_000_000), "quote": (8, usd, 0, 0), "quote_mint": WBTC})
    real_build = L.dispatch_build({"pump_curve": b})

    def build(bb, clip, snap, **kw):                   # the first leg's build is over the packet
        if not L.requote_attempts(bb):
            return None, "transaction is 1293 bytes, over the 1232-byte packet"
        return real_build(bb, clip, snap, **kw)
    sent, offers = {}, []

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        from sniper.execution.submission_transport import entry_slot_refusal
        assert entry_head_fn()[0] == buy.slot + 100
        assert entry_slot_refusal(None, entry_head_fn, clock=lambda: now) is None
        sent.update(validity=validity)
        return {"status": "filled", "position_id": 1, "sig": "TXQ", "quote_residue_atomic": 5}
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    snap = L.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5, blockhash=BLOCKHASH,
                              last_valid_height=1, observed_mono={"sol_usd": now - 1, "balances": now - 1,
                                                                  "blockhash": now - 1})
    legs = L.PendingLegs()
    legs.add(buy, deadline=now + L.MAX_SIGNAL_AGE_S)
    lane = L.SlotLane(conn=c, executor={"slot_execute_fn": execute}, build_fn=build, snapshot_fn=lambda: snap,
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                      now_iso_fn=lambda: "2026-10-07T21:26:11+00:00", lock_key="sol-test", mode="live",
                      pending_legs=legs, shadow_clip_fn=lambda: 10.0,
                      exotic_offer=lambda bb, **kw: offers.append((bb, kw)) or True,
                      head_slot_fn=lambda: (buy.slot + 100, now),
                      clock=SimpleNamespace(monotonic=lambda: now, perf_counter=time.perf_counter))
    with contextlib.closing(c):
        first = lane.handle(buy)
        assert first["reason"] == L.REQUOTE_PENDING_REASON and sent == {}
        (offered, kw), = offers
        assert kw["quote_params"] == "&maxAccounts=20"
        assert legs.holds(buy.signature, now=now + 1.0)          # still owned, moved out
        # The worker's hand-back: a fresh leg on the tighter shape, 0.7 s after the receipt.
        back = dataclasses.replace(buy, recv_monotonic=now - 0.7, ondemand=dict(
            buy.ondemand, leg=_curve_leg(spend_q=11_809, usdc=10_000_000), requote_attempts=1,
            requoted="&maxAccounts=20", requote_offered_mono=now - 0.2))
        second = lane.handle(back)
        assert second["status"] == "filled", second
        assert not legs.holds(buy.signature, now=now)             # claimed after the fill
        rows = c.execute("SELECT terminal_status, terminal_reason, trace_json FROM memecoin_entry_attempts"
                         " ORDER BY decision_started_at, rowid").fetchall()
        assert [r["terminal_status"] for r in rows] == ["rejected", "filled"]
        traces = [{e["name"]: e for e in json.loads(r["trace_json"])["substage_events"]} for r in rows]
        assert traces[0]["slot_requote"]["bytes_before"] == 1293
        result = traces[1]["slot_requote_result"]
        assert result["outcome"] == "fit" and result["bytes_after"] <= 1232 and result["requote_ms"] >= 150

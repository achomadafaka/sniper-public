"""Exotic-quote buys LIVE on the slot lane, E1: CPMM and LaunchLab (spec
docs/superpowers/specs/2026-10-02-memecoin-slot-lane-exotic-quotes-live-design.md; operator GO
2026-10-02 12:0xZ via Alpha; Gamma's review M1/M2/M3, LOW1/LOW2).

The on-demand leg worker quotes a USDC -> quote leg on the FUNDED seam for a venue on
MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES, prices the quote from it, runs admission, and hands
(buy, leg, quote) back to the lane thread, which runs the ordinary live path. A routed buy reaches
the on-chain lane at most once and never zero times: PendingLegs holds the signature (the on-chain
lane defers), and exactly one of the worker's refusal, the lane's re-entry or the expiry sweep
resolves it.
"""
import contextlib
import dataclasses
import sqlite3
import time
from types import SimpleNamespace

import pytest

from sniper.execution import raydium_cpmm_buy as C
from sniper.execution import raydium_launchlab_buy as LL
from sniper.services import memecoin_slot_lane as L
from test_slot_lane_cpmm import (BLOCKHASH, PEPE_EV, USDC_EV, USER, _Routed, _cache,
                                 _exotic_builder, _paper, _routed)


@pytest.fixture
def exotic_cpmm(monkeypatch):
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES",
                        frozenset({"raydium_cpmm"}), raising=False)


def _buy(ev=PEPE_EV, recv=None):
    buy = L.cpmm_slot_buy(_routed(ev))
    return dataclasses.replace(buy, recv_monotonic=time.monotonic() if recv is None else recv)


# --- which buys go live ------------------------------------------------------------------------

def test_an_exotic_quote_is_live_only_on_a_listed_live_venue(monkeypatch):
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    assert L.shadow_only(_buy()) is True                       # today: exotic stays shadow
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES",
                        frozenset({"raydium_cpmm"}), raising=False)
    assert L.exotic_live("raydium_cpmm") and L.shadow_only(_buy()) is False
    monkeypatch.setattr(C, "cpmm_live", lambda: False)          # the venue's own mode decides first
    assert not L.exotic_live("raydium_cpmm") and L.shadow_only(_buy()) is True


def test_a_launchlab_buy_with_an_unknown_quote_stays_shadow(monkeypatch):
    monkeypatch.setattr(LL, "launchlab_live", lambda: True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES",
                        frozenset({"raydium_launchlab"}), raising=False)
    unknown = SimpleNamespace(venue="raydium_launchlab", quote_mint=None, venue_event=None)
    exotic = SimpleNamespace(venue="raydium_launchlab", quote_mint="Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh",
                             venue_event=None)
    assert L.shadow_only(unknown) is True and L.shadow_only(exotic) is False


def test_a_live_exotic_routed_buy_goes_to_the_lane_and_writes_no_routed_row(exotic_cpmm):
    rows, put = _Routed(), []
    L.route_routed_buy(_routed(PEPE_EV), handoff=rows, log=lambda line: None, put=put.append,
                       amm_pools=None, cpmm_on=True, pump_on=False, lane_live=True)
    assert rows.written == [] and [b.venue for b in put] == ["raydium_cpmm"]


# --- PendingLegs: one claim, a deadline -------------------------------------------------------

def test_pending_legs_hold_until_the_deadline_and_resolve_once():
    legs = L.PendingLegs()
    buy = SimpleNamespace(signature="S1")
    assert legs.add(buy, deadline=10.0) and not legs.add(buy, deadline=10.0)   # one entry per sig
    assert legs.holds("S1", now=9.9) and not legs.holds("S1", now=10.0)
    assert legs.claim("S1") is buy and legs.claim("S1") is None              # single claim
    assert not legs.holds("S1", now=0.0)


def test_the_expiry_sweep_writes_each_expired_entry_once():
    legs, written = L.PendingLegs(), []
    legs.add(SimpleNamespace(signature="OLD"), deadline=5.0)
    legs.add(SimpleNamespace(signature="NEW"), deadline=50.0)
    assert L.sweep_expired_legs(legs, now=6.0, fallback_fn=lambda b: written.append(b.signature)) == 1
    assert L.sweep_expired_legs(legs, now=7.0, fallback_fn=lambda b: written.append(b.signature)) == 0
    assert written == ["OLD"] and legs.holds("NEW", now=7.0)


def test_the_on_chain_lane_defers_while_a_leg_is_pending(monkeypatch, tmp_path):
    """Gamma M2: no slotlane:<sig> row exists until the lane reserves, so the registry answers."""
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_onchain_lane as O
    c = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(c)
    legs = L.PendingLegs()
    monkeypatch.setattr(L, "EXOTIC_PENDING", legs)
    detected = "2026-10-02T12:00:00+00:00"
    assert O.slot_lane_outcome(c, "SIGX", detected) is None
    legs.add(SimpleNamespace(signature="SIGX"), deadline=time.monotonic() + 30)
    assert O.slot_lane_outcome(c, "SIGX", detected) == "in_flight"
    legs.claim("SIGX")
    assert O.slot_lane_outcome(c, "SIGX", detected) == "failed"    # released, no attempt: one fallback


# --- the lane thread: offer, re-entry, aged out -------------------------------------------------

class _Wake:
    def __init__(self):
        self.n = 0

    def set(self):
        self.n += 1


def _bare_lane(**kw):
    return L.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=lambda: SimpleNamespace(sol_usd=150.0),
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False, now_iso_fn=None,
                      lock_key="k", mode="live", **kw)


@pytest.mark.parametrize("path", ["exotic", "graduation", "offer_refused", "worker_refused"])
def test_lane_releases_expire_in_the_lane_clock_domain(monkeypatch, path, exotic_cpmm):
    legs = L.PendingLegs()
    monkeypatch.setattr(L, "GRADUATION_PENDING", legs)
    lane = _bare_lane(pending_legs=legs, exotic_offer=lambda b: False,
                      clock=SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(lane, "_handle", lambda *a, **k: {"status": "rejected", "reason": "leg_failed"})
    monkeypatch.setattr(lane, "_routed_fallback", lambda *a: None)
    monkeypatch.setattr(lane, "_exotic_fallback", lambda *a: None)
    buy = SimpleNamespace(signature="CLOCK", recv_monotonic=99.9, source_user_id="u1",
                          venue="raydium_cpmm", ondemand={"requeued_mono": 99.95},
                          graduation={"trigger": "source_completed", "pool": "P"})
    if path == "offer_refused":
        buy.ondemand = buy.graduation = None
        lane._exotic_handoff(buy, "slot_lane_cpmm_quote_not_supported")
    elif path == "worker_refused":
        legs.add(buy, deadline=101.0)
        lane.resolve_exotic_refusal(SimpleNamespace(buy=buy, reason="leg_failed"))
    else:
        legs.add(buy, deadline=101.0)
        getattr(lane, "_handle_requeued" if path == "exotic" else "_handle_graduation")(buy)
    assert not len(legs)
    assert legs.released("CLOCK", now=100.0 + legs.RELEASE_TTL_S - 0.1)
    assert not legs.released("CLOCK", now=100.0 + legs.RELEASE_TTL_S)


def _refusing_admission(monkeypatch, reason="slot_lane_cpmm_quote_not_supported"):
    monkeypatch.setattr(L, "stale_input", lambda snap, now: None)
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: (reason, {}))


def test_a_live_exotic_refusal_goes_to_the_worker_and_not_to_the_on_chain_lane(exotic_cpmm, monkeypatch):
    _refusing_admission(monkeypatch)
    legs, rows, offered = L.PendingLegs(), _Routed(), []
    lane = _bare_lane(routed_handoff=rows, pending_legs=legs,
                      exotic_offer=lambda b: offered.append(b.signature) or True)
    buy = _buy()
    result = lane.handle(buy)
    assert result["reason"] == "slot_lane_exotic_leg_pending" and offered == ["SIG"]
    assert rows.written == [] and legs.holds("SIG", now=time.monotonic())
    assert not legs.holds("SIG", now=buy.recv_monotonic + L.MAX_SIGNAL_AGE_S)   # bounded by signal age


def test_a_full_worker_writes_the_routed_row_at_once(exotic_cpmm, monkeypatch):
    """Gamma M1: a failed offer must never leave a routed buy with no lane."""
    _refusing_admission(monkeypatch)
    legs, rows = L.PendingLegs(), _Routed()
    lane = _bare_lane(routed_handoff=rows, pending_legs=legs, exotic_offer=lambda b: False)
    result = lane.handle(_buy())
    assert result["reason"] == "slot_lane_cpmm_quote_not_supported" and rows.written == ["SIG"]
    assert len(legs) == 0


def test_a_shadow_exotic_venue_never_registers_a_leg(monkeypatch):
    monkeypatch.setattr(C, "cpmm_live", lambda: True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    _refusing_admission(monkeypatch)
    legs = L.PendingLegs()
    lane = _bare_lane(routed_handoff=_Routed(), pending_legs=legs,
                      exotic_offer=lambda b: pytest.fail("a shadow venue reached the live worker"))
    assert lane.handle(_buy())["reason"] == "slot_lane_cpmm_quote_not_supported" and len(legs) == 0


def test_a_requeued_buy_whose_claim_the_sweep_took_sends_nothing(exotic_cpmm, monkeypatch):
    """Gamma: the item expired while still queued; the sweep already wrote its routed row."""
    monkeypatch.setattr(L, "admission_refusal",
                        lambda b, **k: pytest.fail("an aged-out buy reached admission"))
    legs, rows = L.PendingLegs(), _Routed()
    lane = _bare_lane(routed_handoff=rows, pending_legs=legs)
    buy = dataclasses.replace(_buy(), ondemand={"leg": {}, "quote": (6, 1.0, 0, 0)})
    result = lane.handle(buy)                                    # never registered = already claimed
    assert result == {"status": "rejected", "reason": "slot_lane_exotic_aged_out"}
    assert rows.written == [] and lane.exotic_stats["aged_out"] == 1


def _exotic_live_lane(tmp_path, monkeypatch, execute, legs, wake):
    # Source initiation is tested with real receipts in test_memecoin_solana_source_initiation.
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    from sniper.execution import exec_lock
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {}, raising=False)
    builder = _exotic_builder(PEPE_EV)
    calls = []
    out = PEPE_EV.input_amount // 10
    from sniper.execution.pump_curve_buy import prefetch_usdc_sol_leg
    leg = prefetch_usdc_sol_leg(10_000_000, 150, user=USER, jup_call=_paper(calls, out), rpc=None,
                                alt_cache={}, output_mint=PEPE_EV.input_mint, unwrap=False)
    quote = (6, 10.0 / (out / 1e6), 0, 0)
    seen = {}
    real_admission = L.admission_refusal

    def admission(b, **k):
        seen["venue_quote"] = k.get("venue_quote")
        return real_admission(b, **k)
    monkeypatch.setattr(L, "admission_refusal", admission)
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    # ONE STILL CLOCK for the lane, the builder and the leg (Gamma's full run 2026-10-15: 6 reds
    # under load, leg_stale / slot_lane_exotic_aged_out). The 3 s leg age and the 0.5 s signal age
    # measure the scenario, never how long a loaded host took to build this ledger.
    now = time.monotonic()
    clock = SimpleNamespace(monotonic=lambda: now + 0.02, perf_counter=time.perf_counter)   # 20 ms on
    leg["fetched_at"] = now
    builder.clock = clock.monotonic
    snap = L.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5, blockhash=BLOCKHASH,
                              last_valid_height=1, observed_mono={"sol_usd": now - 1, "balances": now - 1,
                                                                  "blockhash": now - 1})
    lane = L.SlotLane(conn=c, executor={"slot_execute_fn": execute},
                      build_fn=L.dispatch_build({"raydium_cpmm": builder}), snapshot_fn=lambda: snap,
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                      now_iso_fn=lambda: "2026-10-02T12:00:00+00:00", lock_key="sol-test", mode="live",
                      routed_handoff=_Routed(), pending_legs=legs, wake=wake,
                      shadow_clip_fn=lambda: 10.0, cpmm_pools=builder.pools, clock=clock)
    buy = dataclasses.replace(_buy(recv=now), ondemand={"leg": leg, "quote": quote,
                                                        "quote_mint": PEPE_EV.input_mint})
    return lane, buy, seen, contextlib.closing(c)


def test_a_requeued_exotic_buy_runs_the_live_path_with_its_leg_and_records_its_residue(
        exotic_cpmm, monkeypatch, tmp_path):
    legs, wake, sent = L.PendingLegs(), _Wake(), {}

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        sent.update(validity=validity, pending=dict(pending))
        assert legs.holds("SIG", now=lane.clock.monotonic()), "the on-chain lane must defer during the send"
        return {"status": "filled", "position_id": 1, "sig": "TXE", "quote_residue_atomic": 1234}
    lane, buy, seen, closing = _exotic_live_lane(tmp_path, monkeypatch, execute, legs, wake)
    legs.add(buy, deadline=buy.recv_monotonic + L.MAX_SIGNAL_AGE_S)
    with closing as c:
        result = lane.handle(buy)
        assert result["status"] == "filled", result
        assert seen["venue_quote"] == (6, buy.ondemand["quote"][1])          # admission priced it
        assert sent["validity"]["residue_quote_mint"] == PEPE_EV.input_mint
        assert lane._venue_live_sends["raydium_cpmm:exotic"] and not lane._venue_live_sends["raydium_cpmm"]
        assert len(legs) == 0 and wake.n >= 1                                  # released, then woken
        row = c.execute("SELECT quote_mint, residue_atomic, venue, entry_sig FROM memecoin_quote_residue"
                        ).fetchone()
        assert (row[0], int(row[1]), row[2], row[3]) == (PEPE_EV.input_mint, 1234, "raydium_cpmm", "TXE")
        assert lane.exotic_stats["sent"] == 1 and lane.exotic_stats["reentry_age_ms"]
        assert lane._canary == {"raydium_cpmm:exotic": 1}                  # its own five canary lines


def test_the_exotic_canary_cap_is_two_an_hour_and_apart_from_the_venues_own(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR", 6)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {}, raising=False)
    assert L.venue_live_cap("raydium_cpmm:exotic") == L.EXOTIC_CANARY_CAP == 2
    lane = _bare_lane()
    exotic = dataclasses.replace(_buy(), ondemand={"leg": {}, "quote": (6, 1.0, 0, 0)})
    lane._venue_live_sends["raydium_cpmm:exotic"].extend([time.monotonic() - 5] * 2)
    assert lane._venue_live_capped(exotic) == "slot_lane_venue_live_cap"
    assert lane._venue_live_capped(L.cpmm_slot_buy(_routed(USDC_EV))) is None
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE",
                        L.cfg._parse_venue_caps("raydium_cpmm:exotic=6"), raising=False)
    assert L.venue_live_cap("raydium_cpmm:exotic") == 6


# --- the worker, live -------------------------------------------------------------------------

def _live_worker(builder, monkeypatch, *, out, admission=lambda buy, q: None, cap_open=True, rpc_reads=None,
                 impact="0"):
    import sniper.execution.pump_curve_buy as P
    funded, requeued, refused = [], [], []
    paper = _paper([], out)

    def funded_call(method, path, body=None, *, http=None):
        funded.append(path)
        status, payload = paper(method, path, body)
        if path.startswith("/quote"):
            payload = dict(payload, priceImpactPct=impact)
        return status, payload
    monkeypatch.setattr(P, "_funded_jup_call", funded_call)
    monkeypatch.setattr(LL, "paper_jup_call", lambda *a, **k: pytest.fail("a live leg used the paper key"))
    # The live re-read of the quote mint answers what the pool cache holds (a test may replace it).
    value = builder.pools._facts[PEPE_EV.pool]["mint_values"][PEPE_EV.input_mint]
    builder.pools.rpc = lambda method, params=None: {"value": value}
    w = LL.OnDemandLegWorker(
        builders={"raydium_cpmm": builder}, admission_fn=admission,
        snapshot_fn=lambda: SimpleNamespace(sol_usd=122.5, blockhash=BLOCKHASH, last_valid_height=1),
        clip_fn=lambda: 10.0, offer_fn=lambda b, r: None, start=False,
        live_fn=lambda venue: True, requeue_fn=requeued.append,
        refuse_fn=lambda b, reason: refused.append(reason), cap_open_fn=lambda venue: cap_open)
    return w, funded, requeued, refused


def test_a_live_leg_is_quoted_on_the_funded_seam_and_handed_back_to_the_lane(monkeypatch):
    b = _exotic_builder(PEPE_EV)
    out = PEPE_EV.input_amount // 10
    seen = []
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=out, impact="0.02",
                                                admission=lambda buy, q: seen.append(q))
    r = w.process(_buy())
    assert r["status"] == "requeued" and len(funded) == 2 and refused == []
    (buy,) = requeued
    paid = 10.0 / (out / 1e6)
    assert buy.ondemand["quote"][:2] == (6, pytest.approx(paid * 0.98))       # Gamma LOW1: impact out
    assert buy.ondemand["quote_usd_paid"] == pytest.approx(paid) and seen[0][1] == pytest.approx(paid * 0.98)
    assert buy.ondemand["leg"]["output_mint"] == PEPE_EV.input_mint and w.stats["requeued"] == 1


@pytest.mark.parametrize("case", ["cap", "admission", "leg"])
def test_every_live_worker_refusal_reaches_refuse_fn(monkeypatch, case):
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(
        b, monkeypatch, out=0 if case == "leg" else PEPE_EV.input_amount // 10, cap_open=case != "cap",
        admission=(lambda buy, q: "mcap $1 over ceiling $0") if case == "admission" else (lambda buy, q: None))
    r = w.process(_buy())
    assert requeued == [] and len(refused) == 1 and r["status"] != "requeued"
    if case == "cap":
        assert funded == [] and refused == ["slot_lane_exotic_cap"]          # no funded call spent


def test_a_live_leg_refuses_a_quote_with_a_transfer_fee(monkeypatch):
    """Gamma LOW2: live takes no fee-bearing quote; shadow still builds it."""
    b = _exotic_builder(PEPE_EV, quote=(6, 300, 2**64 - 1))
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    w.process(_buy())
    assert refused == ["slot_lane_cpmm_quote_fee_bearing"] and funded == [] and requeued == []


@pytest.mark.parametrize("read_at,reads", [(999.0, 0), (None, 1), (700.0, 1), (1001.0, 1),
                                          (float("nan"), 1), ("999", 1), (True, 1)])
def test_live_quote_reuses_only_a_fresh_chain_read(monkeypatch, read_at, reads):
    b = _exotic_builder(PEPE_EV)
    b.pools._facts[PEPE_EV.pool]["quote_read_at"] = read_at
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    w.clock = SimpleNamespace(monotonic=lambda: 1000.0)
    calls = []
    # A required re-read observes a newly paused mint and refuses before quoting/signing.
    b.pools.rpc = lambda method, params: calls.append(method) or {"value": {
        "data": {"parsed": {"info": {"decimals": 6, "extensions": [
            {"extension": "pausableConfig", "state": {"paused": True}}]}}}}}
    result = w.process(_buy())
    assert len(calls) == reads
    if reads:
        assert refused == ["slot_lane_cpmm_quote_unmovable"] and funded == [] and requeued == []
    else:
        assert result["status"] == "requeued" and len(funded) == 2 and refused == []


def test_live_quote_validates_the_value_published_with_its_read_stamp(monkeypatch):
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    w.clock = SimpleNamespace(monotonic=lambda: 1000.0)
    old = b.ondemand_quote

    def publish_new_facts(buy):
        answer = old(buy)
        b.pools._facts[PEPE_EV.pool] = {**b.pools._facts[PEPE_EV.pool], "quote_read_at": 999.0,
            "mint_values": {PEPE_EV.input_mint: {"data": {"parsed": {"info": {
                "decimals": 6, "extensions": [{"extension": "pausableConfig", "state": {"paused": True}}]
            }}}}}}
        return answer                       # the old passing tuple must not use the new read stamp
    b.ondemand_quote = publish_new_facts
    b.pools.rpc = lambda *a: pytest.fail("fresh chain facts need no duplicate RPC")
    w.process(_buy())
    assert refused == ["slot_lane_cpmm_quote_unmovable"] and funded == [] and requeued == []


def test_a_live_leg_rereads_a_quote_mint_older_than_five_minutes(monkeypatch):
    b = _exotic_builder(PEPE_EV)
    reads = []

    def rpc(method, params=None):
        reads.append(method)
        return {"value": {"data": {"parsed": {"info": {"supply": "1", "decimals": 6, "extensions": [
            {"extension": "pausableConfig", "state": {"paused": True}}]}}}}}

    class Clock:
        t = 1000.0

        @classmethod
        def monotonic(cls):
            return cls.t
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    b.pools.rpc = rpc
    w.clock = Clock
    w.process(_buy())
    assert reads == ["getAccountInfo"] and refused == ["slot_lane_cpmm_quote_unmovable"]
    Clock.t += 10                                     # inside the window: the cached verdict stands
    w.process(_buy())
    assert reads == ["getAccountInfo"] and refused[-1] == "slot_lane_cpmm_quote_unmovable"


def test_a_shadow_venue_keeps_the_paper_key_beside_a_live_one(monkeypatch):
    """Gamma M3 / rule 2026-09-27: no shadow path calls the funded seam."""
    import sniper.execution.pump_curve_buy as P
    monkeypatch.setattr(P, "_funded_jup_call", lambda *a, **k: pytest.fail("shadow reached the funded seam"))
    paper = []
    monkeypatch.setattr(LL, "paper_jup_call",
                        lambda m, p, b=None: paper.append(p) or _paper([], PEPE_EV.input_amount // 10)(m, p, b))
    b = _exotic_builder(PEPE_EV)
    w = LL.OnDemandLegWorker(builders={"raydium_cpmm": b}, admission_fn=lambda buy, q: None,
                             snapshot_fn=lambda: SimpleNamespace(sol_usd=122.5, blockhash=BLOCKHASH,
                                                                 last_valid_height=1),
                             clip_fn=lambda: 10.0, offer_fn=None, start=False,
                             live_fn=lambda venue: venue == "raydium_launchlab")
    assert w.process(_buy())["reason"].startswith("built") and len(paper) == 2


# --- the executor reads the residue from the receipt ------------------------------------------

def test_the_slot_executor_reads_the_quote_residue_from_its_own_receipt(monkeypatch, tmp_path):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from sniper.execution import solana_real_seams as seams, solana_slot_executor as slot
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    kp = Keypair()
    owner = str(kp.pubkey())
    q = PEPE_EV.input_mint
    # Manual receipt postprocessing has no automatic source-slot or transport requirement.
    pid = m.reserve_pending(c, mint="coin", source_user_id="wallet", strategy="alpha_b", notional=10.0,
                            reserved_at="2026-10-02T12:00:00Z", network_id="solana", signal_source="manual")

    def cached(_read):
        def receipt(sig):
            return {"meta": {"err": None, "preTokenBalances": [
                {"mint": q, "owner": owner, "uiTokenAmount": {"amount": "100"}}],
                "postTokenBalances": [{"mint": q, "owner": owner, "uiTokenAmount": {"amount": "1334"}}]}}
        return receipt
    monkeypatch.setattr(seams, "successful_receipt_cache", cached)
    monkeypatch.setattr(slot, "execute_prebuilt_entry",
                        lambda _c, **kw: {"status": "filled", "sig": kw["sig"], "position_id": 1})
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url="http://unused")
    msg = MessageV0.try_compile(kp.pubkey(), [], [], Hash.default())
    result = seam["slot_execute_fn"](c, m.pending_row(c, pid), msg,
                                     validity={"residue_quote_mint": q}, now_iso="2026-10-02T12:00:01Z")
    assert result["quote_residue_atomic"] == 1234
    plain = seam["slot_execute_fn"](c, m.pending_row(c, pid), msg, validity={}, now_iso="2026-10-02T12:00:01Z")
    assert "quote_residue_atomic" not in plain


# --- every fallback is written on the lane thread (the handoff's one connection) -----------------

def test_a_worker_refusal_is_resolved_on_the_lane_thread_once(exotic_cpmm):
    legs, rows, wake = L.PendingLegs(), _Routed(), _Wake()
    lane = _bare_lane(routed_handoff=rows, pending_legs=legs, wake=wake)
    buy = _buy()
    legs.add(buy, deadline=time.monotonic() + 30)
    assert lane.resolve_exotic_refusal(L.ExoticRefusal(buy, "mcap $1 over ceiling $0")) is True
    assert rows.written == ["SIG"] and wake.n == 1 and lane.exotic_stats["worker_refused"] == 1
    assert lane.resolve_exotic_refusal(L.ExoticRefusal(buy, "again")) is False   # claimed once
    assert rows.written == ["SIG"]


def test_the_lane_expires_a_held_leg_and_a_late_requeue_then_sends_nothing(exotic_cpmm, monkeypatch):
    monkeypatch.setattr(L, "admission_refusal", lambda b, **k: pytest.fail("aged-out buy admitted"))
    legs, rows, wake = L.PendingLegs(), _Routed(), _Wake()
    lane = _bare_lane(routed_handoff=rows, pending_legs=legs, wake=wake)
    buy = _buy(recv=time.monotonic() - 1.0)                      # already past its signal age
    legs.add(buy, deadline=buy.recv_monotonic + L.MAX_SIGNAL_AGE_S)
    assert lane.expire_exotic(time.monotonic()) == 1 and rows.written == ["SIG"] and wake.n == 1
    late = dataclasses.replace(buy, ondemand={"leg": {}, "quote": (6, 1.0, 0, 0)})
    assert lane.handle(late)["reason"] == "slot_lane_exotic_aged_out" and rows.written == ["SIG"]
    assert lane.exotic_stats["expired"] == 1


def test_a_lane_own_buy_has_no_routed_row_but_still_wakes_the_on_chain_lane(exotic_cpmm):
    legs, rows, wake = L.PendingLegs(), _Routed(), _Wake()
    lane = _bare_lane(routed_handoff=rows, pending_legs=legs, wake=wake)
    own = SimpleNamespace(signature="OWN", venue="pump_amm", routed=None, recv_monotonic=time.monotonic())
    legs.add(own, deadline=time.monotonic() + 30)
    assert lane.resolve_exotic_refusal(L.ExoticRefusal(own, "slot_lane_exotic_cap")) is True
    assert rows.written == [] and wake.n == 1


def test_the_workers_cap_check_reads_the_lanes_sends_without_changing_them(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {}, raising=False)
    lane = _bare_lane()
    now = time.monotonic()
    lane._venue_live_sends["raydium_cpmm:exotic"].extend([now - 4000.0, now - 5.0])
    assert lane.exotic_cap_open("raydium_cpmm") is True                    # one inside the hour
    assert len(lane._venue_live_sends["raydium_cpmm:exotic"]) == 2          # never popped here
    lane._venue_live_sends["raydium_cpmm:exotic"].append(now - 1.0)
    assert lane.exotic_cap_open("raydium_cpmm") is False


# --- the production wiring (start_slot_lane, no threads started) --------------------------------

def _start(monkeypatch, exotic, *, entry_slot_guard_ready=False):
    import json as _json
    from sniper.settings import memecoin as cfg

    import queue as _queue
    import threading as _threading
    real_thread = _threading.Thread
    stop = _threading.Event()

    class Stop(BaseException):
        pass

    class StoppableQueue(_queue.SimpleQueue):
        """The lane loop's queue; once `stop` is set its next get ends the loop's thread, so no test
        leaves a lane polling behind it."""
        def get(self, block=True, timeout=None):
            if stop.is_set():
                raise Stop
            return super().get(block, timeout)
    monkeypatch.setattr(_queue, "SimpleQueue", StoppableQueue)

    class Thread:
        def __init__(self, *, target, name, daemon):
            targets[name] = target

        def start(self):
            pass
    monkeypatch.setattr("threading.Thread", Thread)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM", "live")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_CPMM_ONDEMAND_LEG", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(exotic), raising=False)
    lanes, targets, puts = [], {}, []
    real = L.SlotLane

    def capture(**kw):
        lanes.append(real(**kw))
        return lanes[-1]
    monkeypatch.setattr(L, "SlotLane", capture)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key="fixture", entry_slot_guard_ready=entry_slot_guard_ready)
    lines = []
    status = L.start_slot_lane(
        rc, builders={"pump_curve": object(), "raydium_cpmm": C.SlotLaneCpmmBuilder(user=USER, rpc=None, leg_fn=None)},
        signal_start_fn=lambda on_buy, **k: puts.append(on_buy), sol_usd_fn=lambda: None,
        log=lines.append, roster_path="unused", conn_factory=object, readonly_conn_factory=object)
    _json.dumps(status["exotic"])                                  # the heartbeat can carry it
    def quiet():
        try:
            targets["slot-lane"]()
        except Stop:
            pass

    def run():
        thread = real_thread(target=quiet, daemon=True)
        thread.start()
        return thread
    status["_run"], status["_stop"] = run, stop
    status["_put"], status["_lines"] = puts[0], lines
    return lanes[0], status


def test_a_listed_live_venue_wires_the_worker_into_the_lane(monkeypatch):
    lane, status = _start(monkeypatch, {"raydium_cpmm"})
    assert lane.exotic_offer is not None and status["exotic"] is lane.exotic_stats
    assert "requeued" in status["ondemand_leg"]


def test_no_listed_venue_leaves_the_lane_as_before(monkeypatch):
    lane, status = _start(monkeypatch, set())
    assert lane.exotic_offer is None and status["exotic"]["offered"] == 0


def test_the_lane_loop_resolves_a_worker_refusal_and_sweeps_an_expired_leg(monkeypatch):
    lane, status = _start(monkeypatch, {"raydium_cpmm"})
    lane.pending_legs = legs = L.PendingLegs()
    refused, expired = _buy(), dataclasses.replace(_buy(), signature="OLD")
    legs.add(refused, deadline=time.monotonic() + 30)
    legs.add(expired, deadline=time.monotonic() - 0.01)
    thread = status["_run"]()
    try:
        status["_put"](L.ExoticRefusal(refused, "mcap $1 over ceiling $0"))
        deadline = time.monotonic() + 5
        while (lane.exotic_stats["worker_refused"], lane.exotic_stats["expired"]) != (1, 1):
            assert time.monotonic() < deadline, lane.exotic_stats
            time.sleep(0.01)
    finally:
        status["_stop"].set()
        thread.join(2)
    assert not thread.is_alive() and len(legs) == 0
    assert any("slot lane exotic fallback" in line and "SIG" in line for line in status["_lines"])


# --- Gamma MED-1 (2026-10-02): a hold released without an attempt row gives the deferred on-chain
# detection its ONE fallback; before this fix it waited until slot_fallback_expired (0 attempts) ----

def _held_sweep(monkeypatch):
    from test_memecoin_onchain_lane import _slot_conn, _sweep
    legs = L.PendingLegs()
    monkeypatch.setattr(L, "EXOTIC_PENDING", legs)
    conn = _slot_conn()                                    # the watcher's row for sigS, 5 s old
    calls, deferred = [], {}
    legs.add(SimpleNamespace(signature="sigS", routed=None, venue="raydium_cpmm"),
             deadline=time.monotonic() + 30)
    cursor, results = _sweep(conn, calls, deferred)
    assert calls == [] and len(deferred) == 1
    assert results[0]["result"]["reason"].startswith("slot_lane_exotic_hold sig=sigS hold_left_ms=")
    cursor, _r = _sweep(conn, calls, deferred, cursor=cursor)
    assert calls == [] and len(deferred) == 1              # none while held
    return legs, conn, calls, deferred, cursor, _sweep


@pytest.mark.parametrize("path", ["worker_refusal", "expiry_sweep", "reentry", "reentry_raises"])
def test_a_detection_deferred_on_a_hold_gets_exactly_one_attempt_once_released(exotic_cpmm, monkeypatch,
                                                                               path):
    legs, conn, calls, deferred, cursor, sweep = _held_sweep(monkeypatch)
    lane = _bare_lane(routed_handoff=_Routed(), pending_legs=legs, wake=_Wake())
    buy = dataclasses.replace(_buy(), signature="sigS")
    if path == "worker_refusal":
        assert lane.resolve_exotic_refusal(L.ExoticRefusal(buy, "mcap $1 over ceiling $0"))
    elif path == "expiry_sweep":
        assert lane.expire_exotic(time.monotonic() + 60) == 1
    else:
        requeued = dataclasses.replace(buy, ondemand={"leg": {}, "quote": (6, 1.0, 0, 0)})
        if path == "reentry":
            monkeypatch.setattr(lane, "_handle", lambda b, hand_off=True: {"status": "rejected",
                                                                           "reason": "slot_lane_wallet_busy"})
            lane.handle(requeued)
        else:                                               # Gamma LOW-1: an exception still releases
            monkeypatch.setattr(lane, "_handle", lambda b, hand_off=True: 1 / 0)
            with pytest.raises(ZeroDivisionError):
                lane.handle(requeued)
            assert lane.routed_handoff.written == [buy.routed.signature]  # its routed row came first
    assert len(legs) == 0 and legs.released("sigS", now=time.monotonic())
    sweep(conn, calls, deferred, cursor=cursor)
    assert calls == [("onchain:sigS", "onchain_fallback_after_slot_lane")] and deferred == {}
    sweep(conn, calls, deferred, cursor=cursor)
    assert len(calls) == 1                                  # exactly one


def test_an_attempt_row_still_wins_over_the_release_marker(monkeypatch):
    from test_memecoin_onchain_lane import _slot_attempt
    legs, conn, calls, deferred, cursor, sweep = _held_sweep(monkeypatch)
    _slot_attempt(conn, pending="confirmed")               # the lane filled it
    legs.claim("sigS")
    _c, results = sweep(conn, calls, deferred, cursor=cursor)
    assert calls == [] and deferred == {} and results[0]["result"]["reason"] == "slot_lane_filled"


def test_the_release_marker_lasts_the_on_chain_lookback_and_no_longer():
    legs = L.PendingLegs()
    legs.add(SimpleNamespace(signature="S"), deadline=10.0)
    legs.claim("S", now=100.0)
    assert legs.released("S", now=100.0 + L.PendingLegs.RELEASE_TTL_S - 0.1)
    assert not legs.released("S", now=100.0 + L.PendingLegs.RELEASE_TTL_S)
    assert L.PendingLegs.RELEASE_TTL_S == 120.0                # slot_lane_outcome's lookback


def test_worker_quotes_the_requested_scaled_clip(monkeypatch):
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    result=w.process(_buy(), clip_usd=7.5)
    assert result['status']=='requeued'
    assert requeued[0].ondemand['leg']['usdc_atomic']==7_500_000
    assert requeued[0].ondemand['requested_clip_usd']==7.5
    assert refused==[]


def test_worker_keeps_trial_identity_with_its_requested_leg(monkeypatch):
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    marker = {'trial_id': 1, 'rule': '3a0b141ee', 'clip_usd': 20.0}
    result = w.process(_buy(), clip_usd=20.0, trial=marker)
    assert result['status'] == 'requeued'
    assert requeued[0].ondemand['trial'] == marker
    assert requeued[0].ondemand['leg']['usdc_atomic'] == 20_000_000


def test_scaled_leg_hold_defers_watcher_even_with_a_completed_attempt(monkeypatch, tmp_path):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_onchain_lane as O
    c=sqlite3.connect(tmp_path/'ledger.db')
    m.ensure_memecoin_tables(c)
    detected='2026-10-02T12:00:00+00:00'
    m.start_entry_attempt(c, attempt_id='a1', decision_started_at=detected,
                          origin_event_id='slotlane:SIGX', signal_source='slot_lane')
    c.execute('UPDATE memecoin_entry_attempts SET terminal_status=?, terminal_reason=?, completed_at=? WHERE attempt_id=?',
              ('rejected', L.EXOTIC_PENDING_REASON, detected, 'a1'))
    c.commit()
    legs=L.PendingLegs()
    monkeypatch.setattr(L,'EXOTIC_PENDING',legs)
    legs.add(SimpleNamespace(signature='SIGX'), deadline=time.monotonic()+30)
    assert O.slot_lane_outcome(c,'SIGX',detected)=='in_flight'
    legs.claim('SIGX')
    assert O.slot_lane_outcome(c,'SIGX',detected)=='failed'
    c.close()


def test_scaled_requote_keeps_original_hold_and_cannot_loop(exotic_cpmm):
    buy = dataclasses.replace(_buy(), ondemand={'leg': {'usdc_atomic': 10_000_000},
                                               'quote': (6, 1, 0, 0)})
    offered=[]
    lane=_bare_lane(exotic_offer=lambda b, **kw: offered.append((b,kw)) or True)
    deadline=time.monotonic()+30
    lane.pending_legs.add(buy,deadline=deadline)
    lane._handle=lambda b, **kw: lane._exotic_handoff(b, L.SCALED_CLIP_REASON, clip_usd=7.5) or {'status':'rejected'}
    result=lane.handle(buy)
    assert result.get('exotic_pending') is True
    assert lane.pending_legs.holds(buy.signature,now=time.monotonic())
    assert offered[0][1]=={'clip_usd':7.5}
    assert not lane.pending_legs.holds(buy.signature,now=deadline+1)
    new_buy=dataclasses.replace(buy,ondemand={**buy.ondemand,'requested_clip_usd':7.5,'resized':True})
    assert lane._exotic_handoff(new_buy,L.SCALED_CLIP_REASON,clip_usd=8.0) is None


def test_a_wallet_capped_first_leg_may_still_resize_once(exotic_cpmm, monkeypatch):
    """jpegkite DVypDG8A 02:56Z and 03:01Z (2026-10-07): the first leg came back sized at the wallet
    cap (requested_clip_usd set), the coin classified smaller, and the old rule (resize only without
    a requested clip) refused slot_lane_scaled_clip with the worker idle. One resize is allowed; the
    resized leg cannot ask again."""
    offered = []
    lane = _bare_lane(exotic_offer=lambda b, **kw: offered.append((b, kw)) or True)
    first = dataclasses.replace(_buy(), ondemand={'leg': {'usdc_atomic': 50_000_000},
                                                  'quote': (6, 1, 0, 0), 'requested_clip_usd': 50.0,
                                                  'resized': False})
    lane.pending_legs.add(first, deadline=time.monotonic() + 30)
    result = lane._exotic_handoff(first, L.SCALED_CLIP_REASON, clip_usd=37.5)
    assert result is not None and result.get('exotic_pending') is True
    assert offered[0][1] == {'clip_usd': 37.5}
    second = dataclasses.replace(first, ondemand={**first.ondemand, 'requested_clip_usd': 37.5,
                                                  'resized': True})
    assert lane._exotic_handoff(second, L.SCALED_CLIP_REASON, clip_usd=30.0) is None


def test_the_worker_marks_a_requote_of_a_buy_with_a_leg_as_its_resize(monkeypatch):
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    assert w.process(_buy(), clip_usd=10.0)['status'] == 'requeued'
    assert requeued[-1].ondemand['resized'] is False
    assert w.process(requeued[-1], clip_usd=7.5)['status'] == 'requeued'
    assert requeued[-1].ondemand['resized'] is True and requeued[-1].ondemand['requested_clip_usd'] == 7.5



@pytest.mark.parametrize("resumed", [True, False])
def test_a_receipt_resumed_exotic_buy_runs_its_leg_without_the_released_hold(
        exotic_cpmm, monkeypatch, tmp_path, resumed):
    """2026-10-07 04:59-05:02Z, 3 lost: an SPCX-quoted routed buy came back from the worker with its
    leg, refused missing_receipt_proof (no signer proof yet), and released its PendingLegs hold. The
    confirmed receipt resumed it ~220 ms later, still inside the signal age, and the lane refused
    slot_lane_exotic_aged_out because that hold was gone. A resume is owned by CONFIRMED_PENDING;
    an un-resumed requeue with no hold still ages out."""
    legs, wake, sent = L.PendingLegs(), _Wake(), {}

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        sent.update(validity=validity)
        return {"status": "filled", "position_id": 1, "sig": "TXR", "quote_residue_atomic": 0}
    lane, buy, _seen, closing = _exotic_live_lane(tmp_path, monkeypatch, execute, legs, wake)
    monkeypatch.setattr(L, "CONFIRMED_PENDING", L.PendingLegs())
    if resumed:
        buy = dataclasses.replace(buy, source_initiation={"proof_version": "direct-wallet-paid-v1"})
        L.CONFIRMED_PENDING.add(buy, deadline=float("inf"))       # RoutedHandoff.offer's claim
    with closing:
        result = lane.handle(buy)                          # no PendingLegs hold: the first pass ended
    if resumed:
        assert result["status"] == "filled", result
        assert lane.exotic_stats["aged_out"] == 0 and sent
    else:
        assert result == {"status": "rejected", "reason": L.EXOTIC_AGED_OUT_REASON} and not sent


# --- N11 (Codex follow-up 2026-10-07, F-SOL-1): a receipt-resumed exotic buy and its one resize ----

def _resumed_resize(tmp_path, monkeypatch):
    """A routed exotic CPMM buy whose first leg ($10.00) was sized before its coin classified at
    risk 0.75 ($7.50), resumed by its confirmed receipt through the real RoutedHandoff. Its first
    pass ended missing_receipt_proof, so it has NO PendingLegs hold; CONFIRMED_PENDING owns it."""
    from sniper.services import memecoin_market_state as M
    from sniper.services import memecoin_onchain_lane as O
    from sniper.services import memecoin_source_initiation as I
    sent = []

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        sent.append(validity)
        return {"status": "filled", "position_id": 1, "sig": "TXR", "quote_residue_atomic": 0}
    legs, wake = L.PendingLegs(), _Wake()
    lane, buy, _seen, closing = _exotic_live_lane(tmp_path, monkeypatch, execute, legs, wake)
    for knob in ("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", "MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD",
                 "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", "MEMECOIN_REAL_MAX_SOURCE_BUY_USD",
                 "MEMECOIN_REAL_MAX_MCAP_USD"):
        monkeypatch.setattr(L.cfg, knob, 0.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {})
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_SCALED_SOL", True)
    real_classify = M.classify_funded_entry
    monkeypatch.setattr(M, "classify_funded_entry",
                        lambda *a, **kw: dataclasses.replace(real_classify(*a, **kw), risk_scale=0.75))
    monkeypatch.setattr("sniper.services.memecoin_wallet_caps.entry_cap_usd", lambda *a, **kw: None)
    lane.shadow_clip_fn = lambda: 10.0
    offered = []
    lane.exotic_offer = lambda b, **kw: offered.append(kw) or True
    clock = SimpleNamespace(monotonic=lambda: buy.recv_monotonic + 0.02, perf_counter=time.perf_counter)
    lane.clock = clock
    monkeypatch.setattr(O, "time", SimpleNamespace(monotonic=clock.monotonic))   # O's name only
    monkeypatch.setattr(L, "EXOTIC_PENDING", legs)
    monkeypatch.setattr(L, "CONFIRMED_PENDING", L.PendingLegs())
    monkeypatch.setattr(I, "source_initiation_refusal", lambda *_: None)
    first = dataclasses.replace(buy, ondemand={**buy.ondemand, "resized": False})   # its $10.00 leg
    proof = dict(schema_version=1, proof_version=I.DIRECT_PROOF_VERSION, network_id="solana",
                 tx_hash=first.signature, block_number=first.slot, mint=first.mint, beneficiary=first.owner,
                 payer=first.owner, source_user_id=first.source_user_id,
                 acquired_atomic=str(first.routed.token_delta_atomic),
                 payment_atomic=str(first.routed.usdc_spent_atomic),
                 route_payment_atomic=str(first.routed.usdc_spent_atomic))
    ev = SimpleNamespace(network_id="solana", signal_source="onchain_watch",
                         fomo_event_id="onchain:" + first.signature, source_initiation=proof,
                         source_wallet=first.owner, source_user_id=first.source_user_id,
                         source_block_number=first.slot, mint=first.mint)
    resumed = []
    handoff = L.RoutedHandoff(conn_factory=None, wake=None, put=resumed.append, clock=clock)
    lane.routed_handoff = handoff
    handoff.remember(first)
    assert handoff.offer(ev) and L.CONFIRMED_PENDING.holds(first.signature, now=clock.monotonic())
    resized = dataclasses.replace(resumed[0], ondemand={
        **buy.ondemand, "leg": dict(buy.ondemand["leg"], usdc_atomic=7_500_000),
        "requested_clip_usd": 7.5, "resized": True})
    return SimpleNamespace(lane=lane, first=resumed[0], resized=resized, handoff=handoff, offered=offered,
                           sent=sent, clock=clock, legs=legs, closing=closing, conn=lane.conn,
                           outcome=lambda: O.slot_lane_outcome(lane.conn, first.signature,
                                                               "2026-10-02T12:00:00+00:00"))


def test_a_receipt_resumed_exotic_buy_resizes_once_on_a_transferred_hold(exotic_cpmm, monkeypatch, tmp_path):
    """Codex F-SOL-1 ($50 -> $37.50 in the review; $10.00 -> $7.50 here): the resize asked for the
    first leg's released hold and refused slot_lane_scaled_clip with the worker never offered. The
    resume's claim now moves to a new hold with the ORIGINAL deadline before the offer."""
    s = _resumed_resize(tmp_path, monkeypatch)
    with s.closing:
        result = s.lane.handle(s.first)
        assert result.get("exotic_pending") is True, result
        assert s.offered == [{"clip_usd": 7.5}] and not s.sent
        assert s.legs.hold_left_ms(s.first.signature, s.clock.monotonic()) == pytest.approx(480.0)
        s.handoff.finish(s.first)                                 # the lane loop's finally
        assert not L.CONFIRMED_PENDING.holds(s.first.signature, now=s.clock.monotonic())
        assert s.outcome() == "in_flight"                          # the fallback waits on the hold
        assert s.lane.handle(s.resized)["status"] == "filled" and len(s.sent) == 1
        assert s.lane._exotic_handoff(s.resized, L.SCALED_CLIP_REASON, clip_usd=8.0) is None   # one resize


def test_a_resized_requeue_during_the_claim_overlap_sends_once(exotic_cpmm, monkeypatch, tmp_path):
    """Alpha: the brief window where CONFIRMED_PENDING and the new hold both own the buy. The
    fallback reads in_flight, the resized leg sends once, and a duplicate requeue sends nothing."""
    s = _resumed_resize(tmp_path, monkeypatch)
    with s.closing:
        assert s.lane.handle(s.first).get("exotic_pending") is True
        assert L.CONFIRMED_PENDING.holds(s.first.signature, now=s.clock.monotonic())
        assert s.legs.holds(s.first.signature, now=s.clock.monotonic())
        assert s.outcome() == "in_flight"
        assert s.lane.handle(s.resized)["status"] == "filled" and len(s.sent) == 1
        s.handoff.finish(s.first)
        again = s.lane.handle(s.resized)
        assert again == {"status": "rejected", "reason": L.EXOTIC_AGED_OUT_REASON} and len(s.sent) == 1


@pytest.mark.parametrize("ending", ["worker_refusal", "expiry_sweep"])
def test_a_transferred_resize_hold_ends_in_one_fallback_and_no_late_send(exotic_cpmm, monkeypatch, tmp_path,
                                                                         ending):
    s = _resumed_resize(tmp_path, monkeypatch)
    with s.closing:
        assert s.lane.handle(s.first).get("exotic_pending") is True
        s.handoff.finish(s.first)
        if ending == "worker_refusal":
            assert s.lane.resolve_exotic_refusal(L.ExoticRefusal(s.resized, "slot_lane_cpmm_leg_failed"))
        else:
            assert s.lane.expire_exotic(s.first.recv_monotonic + L.MAX_SIGNAL_AGE_S) == 1
        assert s.outcome() == "failed"                             # exotic_released: its one fallback
        late = s.lane.handle(s.resized)
        assert late == {"status": "rejected", "reason": L.EXOTIC_AGED_OUT_REASON} and not s.sent


def test_a_resumed_requeue_owned_by_nobody_sends_nothing(exotic_cpmm, monkeypatch):
    """The bare 'resumed' bypass let any requeue carrying source_initiation run. A resumed requeue
    runs only while CONFIRMED_PENDING or its PendingLegs hold owns it."""
    monkeypatch.setattr(L, "admission_refusal",
                        lambda b, **k: pytest.fail("an unowned resumed requeue reached admission"))
    monkeypatch.setattr(L, "CONFIRMED_PENDING", L.PendingLegs())
    lane = _bare_lane(routed_handoff=_Routed(), pending_legs=L.PendingLegs())
    buy = dataclasses.replace(_buy(), source_initiation={"proof_version": "direct-wallet-paid-v1"},
                              ondemand={"leg": {}, "quote": (6, 1.0, 0, 0), "resized": True})
    assert lane.handle(buy) == {"status": "rejected", "reason": L.EXOTIC_AGED_OUT_REASON}
    assert lane.exotic_stats["aged_out"] == 1


# --- 5a (2026-10-07): a worker POLICY refusal releases with no fallback class ------------------
# Three live fills 10-02..10-07 were late ordinary purchases of buys the exotic worker had refused
# on policy: the refusal released the hold as exotic_released, a fallback class, so fast-lane-only
# woke an ordinary attempt. The exact refusal strings from logs/memecoin_runner.log:
REAL_POLICY_REFUSALS = [
    "mcap $1035038 over ceiling $1000000",                      # 7KEPApdb frankdegods 10-02 16:19Z
    "mcap $260155 over ceiling $250000",                        # CwSNUU54 ResellCalendar 10-03 00:46Z
    "source buy $99 (0.405%) under mcap floor 0.500% ($122)",   # DVypDG8A jpegkite 10-07 03:26Z
]


@pytest.mark.parametrize("reason, fallback", [(r, False) for r in REAL_POLICY_REFUSALS]
                         + [("slot_lane_cpmm_leg_failed", True), ("slot_lane_exotic_cap", True),
                            ("slot_lane_venue_raydium_cpmm", True)])
def test_a_worker_policy_refusal_never_wakes_a_fast_lane_only_fallback(exotic_cpmm, monkeypatch, reason,
                                                                      fallback):
    from test_memecoin_onchain_lane import approved, now_iso
    from sniper.services.memecoin_onchain_lane import sweep_onchain_detections
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_FAST_LANE_ONLY", True)
    legs, conn, _calls, deferred, cursor, _sweep = _held_sweep(monkeypatch)
    lane = _bare_lane(routed_handoff=_Routed(), pending_legs=legs, wake=_Wake())
    assert lane.resolve_exotic_refusal(L.ExoticRefusal(dataclasses.replace(_buy(), signature="sigS"), reason))
    seen = []

    def hook(_c, ev):
        seen.append((ev.slot_fallback_class, ev.native_failure_fallback))
        return {"status": "rejected", "reason": "native_fast_lane_unavailable"}
    sweep_onchain_detections(conn, hook, cursor=cursor, now_fn=now_iso, max_age_s=45,
                             approved_ids_fn=approved, deferred=deferred)
    assert seen == ([("exotic_released", True)] if fallback else [(None, False)])


def test_a_policy_refusal_after_its_first_pass_row_still_has_no_fallback_class(monkeypatch, tmp_path):
    """The other branch: the first pass wrote its attempt row (reason exotic_leg_pending)."""
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_onchain_lane as O
    c = sqlite3.connect(tmp_path / "ledger.db")
    m.ensure_memecoin_tables(c)
    detected = "2026-10-02T12:00:00+00:00"
    m.start_entry_attempt(c, attempt_id="a1", decision_started_at=detected,
                          origin_event_id="slotlane:SIGX", signal_source="slot_lane")
    c.execute("UPDATE memecoin_entry_attempts SET terminal_status=?, terminal_reason=?, completed_at=? "
              "WHERE attempt_id=?", ("rejected", L.EXOTIC_PENDING_REASON, detected, "a1"))
    legs = L.PendingLegs()
    monkeypatch.setattr(L, "EXOTIC_PENDING", legs)
    lane = _bare_lane(routed_handoff=_Routed(), pending_legs=legs, wake=_Wake())
    for reason, klass in ((REAL_POLICY_REFUSALS[0], None), ("slot_lane_cpmm_leg_failed", "exotic_released")):
        legs.add(SimpleNamespace(signature="SIGX", routed=None), deadline=time.monotonic() + 30)
        assert lane.resolve_exotic_refusal(L.ExoticRefusal(SimpleNamespace(signature="SIGX", routed=None),
                                                           reason))
        assert O.slot_lane_verdict(c, "SIGX", detected) == ("failed", klass)
    c.close()


def test_every_policy_return_of_admission_refusal_is_classified_policy(exotic_cpmm, monkeypatch):
    """A pin on the real producers: reword a policy refusal and this fails, instead of the reworded
    string silently becoming a mechanical refusal with a fallback again."""
    from sniper.services import memecoin_real_runner as R
    buy = _buy()
    assert L.policy_refusal(L.admission_refusal(buy, armed_ids={"u1"}, sol_usd=150.0, kill_tripped=True)[0])
    assert L.policy_refusal(L.admission_refusal(buy, armed_ids=set(), sol_usd=150.0, kill_tripped=False,
                                                cpmm_pools=_cache(PEPE_EV))[0])
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 1.0)
    ceiling, _ = L.admission_refusal(buy, armed_ids={"u1"}, sol_usd=150.0, kill_tripped=False,
                                     cpmm_pools=_cache(PEPE_EV), venue_quote=(6, 1.0))
    assert ceiling and ceiling.startswith("mcap $") and L.policy_refusal(ceiling)
    family = dict(their_usd=10.0, source_mcap=1e6, prior_net_usd=None, ratio=0.0, unknown_mcap_floor=0.0,
                  abs_min=0.0, abs_max=0.0, family_active=True)
    for override in ({"ratio": 0.01}, {"source_mcap": None, "unknown_mcap_floor": 500.0},
                     {"abs_min": 100.0}, {"abs_max": 5.0}):
        reason, _info = R.source_buy_family_reason(**dict(family, **override))
        assert reason and L.policy_refusal(reason), (override, reason)
    for mechanical in (L.admission_refusal(buy, armed_ids={"u1"}, sol_usd=150.0, kill_tripped=False)[0],
                       "slot_lane_cpmm_leg_failed", "slot_lane_exotic_cap", "no_clip", "", None):
        assert not L.policy_refusal(mechanical), mechanical


@pytest.mark.parametrize("trip, how", [(True, "policy"), (False, "claimed")])
def test_a_requeued_leg_the_lane_refuses_on_policy_releases_as_policy(exotic_cpmm, monkeypatch, tmp_path,
                                                                      trip, how):
    """Gamma LOW on 1d1a8a515: the worker's admission passed, then state changed (here the kill
    switch) before the lane re-ran admission on the requeued leg. That refusal comes before any
    attempt row, so its release must carry the policy kind too. A mechanical lane refusal
    (the signal aged out of the build below) still releases as "claimed"."""
    legs, wake = L.PendingLegs(), _Wake()
    lane, buy, _seen, closing = _exotic_live_lane(tmp_path, monkeypatch, lambda *a, **k: pytest.fail("sent"),
                                                  legs, wake)
    if trip:
        lane.kill_tripped_fn = lambda: True
    else:
        monkeypatch.setattr(lane, "_handle", lambda b, hand_off=True: {"status": "rejected",
                                                                       "reason": "slot_lane_wallet_busy"})
    from sniper.services import memecoin_onchain_lane as O
    monkeypatch.setattr(L, "EXOTIC_PENDING", legs)
    legs.add(buy, deadline=time.monotonic() + 30)
    with closing:
        result = lane.handle(buy)
        assert (result["reason"] == "kill_switch") is trip
        assert legs.release_kind(buy.signature, time.monotonic()) == how
        # No attempt row was written: the no-row branch decides, refused with no fallback class.
        assert O.slot_lane_verdict(lane.conn, buy.signature, "2026-10-02T12:00:00+00:00") == (
            "failed", None if trip else "exotic_released")


@pytest.mark.parametrize("cached_at", [998.0, 999.0])
def test_new_unsafe_snapshot_overrides_old_safe_worker_cache(monkeypatch, cached_at):
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    w.clock = SimpleNamespace(monotonic=lambda: 1000.0)
    w._quote_seen[PEPE_EV.input_mint] = (cached_at, (6, 0, 0))
    b.pools._facts[PEPE_EV.pool] = {**b.pools._facts[PEPE_EV.pool], "quote_read_at": 999.0,
        "mint_values": {PEPE_EV.input_mint: {"data": {"parsed": {"info": {"decimals": 6,
            "extensions": [{"extension": "pausableConfig", "state": {"paused": True}}]}}}}}}
    b.pools.rpc = lambda *a: pytest.fail("new bound snapshot needs no duplicate read")
    result = w._live_quote(b, PEPE_EV.input_mint, (6, 0, 0), facts=b.pools._facts[PEPE_EV.pool])
    assert result == (None, "slot_lane_cpmm_quote_unmovable")
    assert w._quote_seen[PEPE_EV.input_mint] == (999.0, None)
    assert funded == [] and requeued == []


def test_failed_quote_refresh_keeps_only_original_expired_stamp(monkeypatch):
    b = _exotic_builder(PEPE_EV)
    w, *_ = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    w.clock = SimpleNamespace(monotonic=lambda: 1000.)
    old = (699., (6, 0, 0))
    w._quote_seen[PEPE_EV.input_mint] = old
    b.pools._facts[PEPE_EV.pool]["quote_read_at"] = 699.
    def failed(*args): raise TimeoutError
    b.pools.rpc = failed
    assert w._live_quote(b, PEPE_EV.input_mint, (6, 0, 0), facts=b.pools._facts[PEPE_EV.pool])[1] == "slot_lane_cpmm_quote_unread"
    assert w._quote_seen[PEPE_EV.input_mint] is old

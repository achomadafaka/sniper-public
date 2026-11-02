"""AGP 2026-10-13: the first migrated pool must not lose an unsigned mismatched cash leg."""
import dataclasses
import sqlite3
import time
from types import SimpleNamespace

import pytest

import test_slot_lane_graduation as G
from sniper.execution import pump_curve_buy as P
from sniper.execution import raydium_launchlab_buy as LL
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_slot_lane as L


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    from sniper.execution import exec_lock
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / (key + ".lock")))
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(L.cfg, "MEMECOIN_TRADE_SIZE_USD", 109.29)
    monkeypatch.setattr(L.cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 400.)
    monkeypatch.setattr(L.cfg, "MEMECOIN_MAX_CONCURRENT", 5)
    monkeypatch.setattr(L.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_WALLET_CAPS_ENABLED", False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET", {})
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", ("pump_curve", "pump_amm"))
    monkeypatch.setattr(L, "lane_mode", lambda: "live")
    holds, legs = L.PendingLegs(), L.PendingLegs()
    monkeypatch.setattr(L, "GRADUATION_PENDING", holds)
    monkeypatch.setattr(L, "EXOTIC_PENDING", legs)
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.commit()
    now = time.monotonic()
    snap = L.PrefetchSnapshot(sol_usd=185., usdc_balance=400., sol_balance=.5,
                             blockhash=G.BLOCKHASH, last_valid_height=1,
                             observed_mono={"sol_usd": now, "balances": now, "blockhash": now})
    amm = G._builder(time.monotonic)
    amm._leg = dict(amm._leg, usdc_atomic=167_630_000)
    amm._cash_legs = (dict(amm._leg, usdc_atomic=125_720_000),
                      dict(amm._leg, usdc_atomic=50_000_000))
    state, _ = G._at_premium(G._state(), .10, 185.)
    grad = G._grad(state)
    grad["requeued_mono"] = now
    buy = G._slot_buy(G.FX["source"]["trade_event_log"], token_program=P.TOKEN_2022_PROGRAM,
                      recv_monotonic=now - 1.5, graduation=grad)
    holds.add(buy, deadline=now + 1.)
    offers, sent, wakes = [], [], []

    def offer(candidate, **kw):
        offers.append((candidate, kw, holds.holds(candidate.signature, time.monotonic()),
                       legs.holds(candidate.signature, time.monotonic())))
        return True

    def execute(conn_, pending, message, **kw):
        sent.append((dict(pending), kw["validity"]))
        m.release_pending(conn_, pending["id"], "chain_error:test_stop")
        return {"status": "failed", "reason": "chain_error:test_stop", "sig": "OWN"}

    lane = L.SlotLane(conn=conn, executor={"slot_execute_fn": execute},
                      build_fn=L.dispatch_build({"pump_amm": amm}), snapshot_fn=lambda: snap,
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                      now_iso_fn=lambda: "2026-10-13T01:23:24+00:00", lock_key="cash-recovery",
                      mode="live", shadow_clip_fn=lambda: 167.63, exotic_offer=offer,
                      pending_legs=legs, wake=SimpleNamespace(set=lambda: wakes.append(
                          (holds.holds("SIG", time.monotonic()), legs.holds("SIG", time.monotonic())))))
    yield SimpleNamespace(lane=lane, buy=buy, conn=conn, offers=offers, sent=sent, holds=holds,
                          legs=legs, amm=amm, snap=snap, wakes=wakes)
    conn.close()


def test_unsigned_graduation_leg_mismatch_holds_exact_repair_before_releasing(recovery):
    r = recovery
    result = r.lane.handle(r.buy)
    assert result.get("exotic_pending") is True, result
    assert not r.sent
    candidate, request, grad_held, leg_held = r.offers[0]
    assert request == {"clip_usd": 109.29}
    assert candidate.signature == r.buy.signature and candidate.recv_monotonic == r.buy.recv_monotonic
    assert grad_held and leg_held
    assert r.wakes[-1] == (False, True) and all(pool or leg for pool, leg in r.wakes)
    assert r.legs.hold_left_ms("SIG", time.monotonic()) <= 500.
    row = dict(r.conn.execute("select status,tx_sig,reserved_notional_usd from memecoin_pending_entries").fetchone())
    assert row == {"status": "failed", "tx_sig": None, "reserved_notional_usd": 109.29}
    assert candidate.graduation["pool"] == G.POOL


def test_unsigned_under_clip_graduation_uses_same_exact_cash_recovery(recovery):
    r = recovery
    r.amm._leg = dict(r.amm._leg, usdc_atomic=50_000_000)
    r.amm._cash_legs = ()
    result = r.lane.handle(r.buy)
    assert result.get("exotic_pending") and r.offers[0][1] == {"clip_usd": 109.29}
    assert not r.sent
    assert r.conn.execute("select resolution_reason from memecoin_pending_entries").fetchone()[0] == (
        "slot_lane_build:leg_under_clip")


def test_exact_repair_reenters_once_with_new_reservation_and_original_source(recovery):
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending") is True
    candidate = r.offers[0][0]
    expected_sol = int(109.29 / 185. * 1e9)
    leg = dict(r.amm._leg, usdc_atomic=109_290_000, fetched_at=time.monotonic(), output_mint=P.WSOL_MINT,
               out_lamports=expected_sol, min_out_lamports=int(expected_sol * .98))
    returned = dataclasses.replace(candidate, ondemand={"leg": leg, "quote": (9, 185., 0, 0),
        "quote_mint": P.WSOL_MINT, "requested_clip_usd": 109.29, "requeued_mono": time.monotonic()})
    result = r.lane.handle(returned)
    assert result["reason"] == "chain_error:test_stop", result
    assert len(r.sent) == 1 and len(r.offers) == 1
    pending, validity = r.sent[0]
    assert pending["reserved_notional_usd"] == 109.29 and pending["origin_event_id"] == "slotlane:SIG"
    assert validity["route_target"].endswith(G.POOL)
    assert not r.legs.holds("SIG", time.monotonic())
    assert r.lane.handle(returned)["reason"] == "slot_lane_graduation_aged_out"
    assert len(r.sent) == 1


def test_worker_quotes_wrapped_sol_on_pumpswap_for_cash_recovery(monkeypatch, recovery):
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending") is True
    candidate = r.offers[0][0]
    queued, requests = [], []
    monkeypatch.setattr(LL.OnDemandLegWorker, "_live_quote", lambda *a, **k: ((9, 0, 0), None))

    def quote(atoms, slippage, **kw):
        requests.append((atoms, kw["unwrap"], kw["output_mint"]))
        return dict(r.amm._leg, usdc_atomic=atoms, output_mint=P.WSOL_MINT, quote={},
                    fetched_at=time.monotonic())

    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", quote)
    worker = LL.OnDemandLegWorker(builders={"pump_curve": SimpleNamespace(
            REASON_PREFIX="curve", ondemand_quote=lambda buy: (None, None, "curve_wrong_builder")),
            "pump_amm": r.amm}, admission_fn=lambda b, q: None, snapshot_fn=lambda: r.snap,
            clip_fn=lambda: 167.63, offer_fn=lambda *a: None, live_fn=lambda v: True,
            requeue_fn=queued.append, start=False)
    result = worker.process(candidate, clip_usd=109.29)
    assert result["status"] == "requeued", result
    assert requests == [(109_290_000, False, P.WSOL_MINT)]
    assert queued[0].graduation["pool"] == G.POOL and queued[0].ondemand["requested_clip_usd"] == 109.29
    assert queued[0].ondemand["resized"] is True  # no further resize loop


@pytest.mark.parametrize("offer", [False, "raise"])
def test_unavailable_cash_worker_releases_unsigned_reservation_once(offer, recovery):
    r = recovery
    def unavailable(*a, **k):
        if offer == "raise":
            raise TimeoutError("worker offer")
        return False
    r.lane.exotic_offer = unavailable
    result = r.lane.handle(r.buy)
    assert result["reason"] == "slot lane build refused: leg_over_clip"
    assert not r.legs.holds("SIG", time.monotonic()) and not r.sent
    assert r.conn.execute("select status,tx_sig from memecoin_pending_entries").fetchone()[:] == ("failed", None)


@pytest.mark.parametrize("finish", ["refusal", "expiry"])
def test_cash_quote_failure_or_expiry_has_one_fallback(finish, recovery):
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    r.wakes.clear()
    if finish == "refusal":
        refusal = L.ExoticRefusal(candidate, "slot_lane_pump_leg_failed")
        assert r.lane.resolve_exotic_refusal(refusal)
        assert not r.lane.resolve_exotic_refusal(refusal)
    else:
        deadline = candidate.graduation["cash_leg_deadline"]
        assert r.lane.expire_exotic(deadline + .001) == 1
        assert r.lane.expire_exotic(deadline + 1) == 0
        assert not r.lane.resolve_exotic_refusal(L.ExoticRefusal(candidate, "worker_timeout"))
    assert len(r.wakes) == 1 and not r.sent


def test_cash_repair_blocks_ordinary_fallback_even_with_packet_requotes_off(monkeypatch, recovery):
    from sniper.services import memecoin_onchain_lane as O
    r = recovery
    monkeypatch.setattr(L, "exotic_requote_mode", lambda: "off")
    assert r.lane.handle(r.buy).get("exotic_pending")
    detected = "2026-10-13T01:23:22+00:00"
    assert O.slot_lane_verdict(r.conn, "SIG", detected) == ("in_flight", None)
    candidate = r.offers[0][0]
    assert r.lane.resolve_exotic_refusal(L.ExoticRefusal(candidate, "slot_lane_pump_leg_failed"))
    assert O.slot_lane_verdict(r.conn, "SIG", detected) == ("failed", "build_refused")


@pytest.mark.parametrize("policy", ["mcap $311151 over ceiling $250000", "kill_switch",
                                    "not_approved", "source buy below floor"])
def test_cash_worker_policy_cannot_revive_old_unsigned_build_failure(policy, recovery):
    from sniper.services import memecoin_onchain_lane as O
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    assert r.lane.resolve_exotic_refusal(L.ExoticRefusal(candidate, policy))
    assert r.legs.release_kind("SIG", time.monotonic()) == "policy"
    assert O.slot_lane_verdict(r.conn, "SIG", "2026-10-13T01:23:22+00:00") == ("decided", None)


@pytest.mark.parametrize("owner,expected", [("reserved", ("in_flight", None)),
    ("confirmed", ("filled", None)), ("signed_unknown", ("in_flight", None)),
    ("signed_failed", ("failed", "landed_fail"))])
def test_cash_worker_policy_never_overrides_durable_funded_owner(owner, expected, recovery):
    from sniper.services import memecoin_onchain_lane as O
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    assert r.lane.resolve_exotic_refusal(L.ExoticRefusal(candidate, "mcap $311151 over ceiling $250000"))
    if owner == "signed_unknown":
        r.conn.execute("update memecoin_entry_attempts set tx_sig='OWN',terminal_status='unknown'")
        r.conn.execute("update memecoin_pending_entries set status='submitted_unknown',tx_sig='OWN'")
    elif owner == "signed_failed":
        r.conn.execute("update memecoin_pending_entries set tx_sig='OWN',resolution_reason='chain_error:test'")
    else:
        r.conn.execute("update memecoin_pending_entries set status=?", (owner,))
    r.conn.commit()
    assert O.slot_lane_verdict(r.conn, "SIG", "2026-10-13T01:23:22+00:00") == expected


@pytest.mark.parametrize("requote", ["off", "oversize"])
def test_verdict_checks_existing_attempt_hold_with_one_lock(requote, monkeypatch, recovery):
    from sniper.services import memecoin_onchain_lane as O
    r = recovery
    monkeypatch.setattr(L, "exotic_requote_mode", lambda: requote)
    # A known unsigned build refusal has no repair release marker; it keeps its fallback.
    r.lane.handle(dataclasses.replace(r.buy, graduation={**r.buy.graduation, "cash_leg_repair": True}))
    acquisitions = []
    original = r.legs._lock
    class Lock:
        def __enter__(self):
            acquisitions.append(1)
            original.acquire()
        def __exit__(self, *a):
            original.release()
    r.legs._lock = Lock()
    assert O.slot_lane_verdict(r.conn, "SIG", "2026-10-13T01:23:22+00:00") == ("failed", "build_refused")
    assert len(acquisitions) == 1


@pytest.mark.parametrize("unsafe", ["unknown_read", "future_read", "signed_hash", "signed_bytes", "native",
                                    "broadcast", "bound"])
def test_cash_repair_cannot_take_unknown_or_signed_funding(unsafe, recovery):
    r = recovery
    buy = r.buy
    pending = {"status": "reserved", "id": 99, "funding_asset": "usdc"}
    if unsafe == "unknown_read":
        buy = dataclasses.replace(buy, graduation={**buy.graduation, "requeued_mono": None})
    elif unsafe == "future_read":
        buy = dataclasses.replace(buy, graduation={**buy.graduation, "requeued_mono": time.monotonic() + 1})
    elif unsafe == "native":
        pending["funding_asset"] = "sol"
    elif unsafe in ("broadcast", "bound"):
        pending["broadcast_at" if unsafe == "broadcast" else "submission_bound_at"] = "bound"
    else:
        pending["signed_payload_sha256" if unsafe == "signed_hash" else "signed_payload_b64"] = "bound"
    trace = SimpleNamespace(mark_substage=lambda *a, **k: pytest.fail("cannot repair"))
    assert r.lane._graduation_cash_repair(buy, 109.29, "leg_over_clip", pending, trace) is None
    assert not r.offers


def test_cash_quote_reentry_preserves_kill_switch(recovery):
    from sniper.services import memecoin_onchain_lane as O
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    r.lane.kill_tripped_fn = lambda: True
    returned = dataclasses.replace(candidate, ondemand={"leg": {"output_mint": P.WSOL_MINT},
        "quote": (9, 185., 0, 0), "quote_mint": P.WSOL_MINT,
        "requested_clip_usd": 109.29, "requeued_mono": time.monotonic()})
    result = r.lane.handle(returned)
    assert result["status"] == "rejected" and "kill" in result["reason"]
    assert not r.sent and r.legs.release_kind("SIG", time.monotonic()) == "policy"
    assert r.conn.execute("select count(*) from memecoin_pending_entries").fetchone()[0] == 1
    assert O.slot_lane_verdict(r.conn, "SIG", "2026-10-13T01:23:22+00:00") == ("decided", None)


def test_cash_quote_reentry_still_requires_source_initiation(monkeypatch, recovery):
    from sniper.services import memecoin_source_initiation as initiation
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *a: "missing_receipt_proof")
    returned = dataclasses.replace(candidate, ondemand={"leg": {"output_mint": P.WSOL_MINT},
        "quote": (9, 185., 0, 0), "quote_mint": P.WSOL_MINT,
        "requested_clip_usd": 109.29, "requeued_mono": time.monotonic()})
    result = r.lane.handle(returned)
    assert result["reason"] == "missing_receipt_proof" and not r.sent
    assert len(r.offers) == 1
    assert r.conn.execute("select count(*) from memecoin_pending_entries").fetchone()[0] == 1


def test_slow_worker_cannot_refresh_pool_read_deadline(monkeypatch, recovery):
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    current = [time.monotonic()]
    clock = SimpleNamespace(monotonic=lambda: current[0])
    monkeypatch.setattr(LL.OnDemandLegWorker, "_live_quote", lambda *a, **k: ((9, 0, 0), None))
    def slow_quote(atoms, slippage, **kw):
        current[0] = candidate.graduation["cash_leg_deadline"] + .001
        return dict(r.amm._leg, usdc_atomic=atoms, output_mint=P.WSOL_MINT, quote={})
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", slow_quote)
    queued, refused = [], []
    worker = LL.OnDemandLegWorker(builders={"pump_curve": object(), "pump_amm": r.amm},
        admission_fn=lambda *a: pytest.fail("aged pool must not re-admit"), snapshot_fn=lambda: r.snap,
        clip_fn=lambda: 167.63, offer_fn=lambda *a: None, live_fn=lambda v: True,
        requeue_fn=queued.append, refuse_fn=lambda b, reason: refused.append(reason),
        clock=clock, start=False)
    result = worker.process(candidate, clip_usd=109.29)
    assert result["reason"] == L.EXOTIC_AGED_OUT_REASON and not queued
    assert refused == [L.EXOTIC_AGED_OUT_REASON]


def test_graduation_cash_worker_uses_actual_pumpswap_policy(monkeypatch, recovery):
    r = recovery
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", ("pump_amm",))
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    queued, policies = [], []
    monkeypatch.setattr(LL.OnDemandLegWorker, "_live_quote", lambda *a, **k: ((9, 0, 0), None))
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", lambda atoms, *a, **kw:
        dict(r.amm._leg, usdc_atomic=atoms, output_mint=P.WSOL_MINT, quote={}))
    worker = LL.OnDemandLegWorker(builders={"pump_curve": object(), "pump_amm": r.amm},
        admission_fn=lambda *a: None, snapshot_fn=lambda: r.snap, clip_fn=lambda: 167.63,
        offer_fn=lambda *a: None, live_fn=lambda v: v == "pump_amm",
        cap_open_fn=lambda v: policies.append(v) or True, requeue_fn=queued.append, start=False)
    result = worker.process(candidate, clip_usd=109.29)
    assert result["status"] == "requeued", result
    assert policies == ["pump_amm"] and len(queued) == 1
    assert L.SlotLane._cap_key(queued[0]) == "pump_amm:exotic"
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", ())
    assert L.shadow_only(queued[0])  # re-entry cannot retain a withdrawn AMM permission


def test_worker_offer_requires_pumpswap_builder_and_keeps_original_buy(recovery):
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    def worker(builders):
        return LL.OnDemandLegWorker(builders=builders, admission_fn=lambda *a: None,
            snapshot_fn=lambda: r.snap, clip_fn=lambda: 167.63, offer_fn=lambda *a: None, start=False)
    missing = worker({"pump_curve": object()})
    assert not missing.offer(candidate, clip_usd=109.29)
    actual = worker({"pump_amm": r.amm})
    assert actual.offer(candidate, clip_usd=109.29)
    assert actual.stats["offered_pump_amm"] == 1
    assert actual._q.get_nowait() == (candidate, 109.29, None, None)


def test_slow_cash_quote_rate_limit_still_cools_shared_worker(monkeypatch, recovery):
    r = recovery
    assert r.lane.handle(r.buy).get("exotic_pending")
    candidate = r.offers[0][0]
    current = [time.monotonic()]
    clock = SimpleNamespace(monotonic=lambda: current[0])
    monkeypatch.setattr(LL.OnDemandLegWorker, "_live_quote", lambda *a, **k: ((9, 0, 0), None))
    monkeypatch.setattr(P, "_funded_jup_call", lambda *a, **kw: (429, {}))
    def limited_quote(atoms, slippage, **kw):
        kw["jup_call"]("GET", "/quote")
        current[0] = candidate.graduation["cash_leg_deadline"] + .001
        return None
    monkeypatch.setattr(P, "prefetch_usdc_sol_leg", limited_quote)
    worker = LL.OnDemandLegWorker(builders={"pump_amm": r.amm}, admission_fn=lambda *a: None,
        snapshot_fn=lambda: r.snap, clip_fn=lambda: 167.63, offer_fn=lambda *a: None,
        live_fn=lambda *a: True, clock=clock, start=False)
    result = worker.process(candidate, clip_usd=109.29)
    assert result["reason"] == r.amm.REASON_PREFIX + "_leg_rate_limited"
    assert worker._backoff_until > current[0] and worker.stats["leg_rate_limited"] == 1


@pytest.mark.parametrize("change", ["ordinary", "wrong_quote", "expired_pool", "already_repaired"])
def test_cash_repair_never_broadens_unrelated_or_stale_buy(change, recovery):
    r = recovery
    buy = r.buy
    if change == "ordinary":
        buy = dataclasses.replace(buy, graduation=None, recv_monotonic=time.monotonic())
    elif change == "wrong_quote":
        buy = dataclasses.replace(buy, graduation={**buy.graduation,
            "pool_facts": {**buy.graduation["pool_facts"], "quote_mint": P.USDC_MINT,
                           "quote_decimals": 6}})
    elif change == "expired_pool":
        buy = dataclasses.replace(buy, graduation={**buy.graduation, "requeued_mono": time.monotonic() - 1})
    else:
        buy = dataclasses.replace(buy, graduation={**buy.graduation, "cash_leg_repair": True})
    r.lane.handle(buy)
    assert not r.offers and not r.sent

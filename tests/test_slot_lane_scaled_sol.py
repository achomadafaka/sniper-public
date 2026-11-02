"""A regime-scaled clip funded in native SOL instead of refused (spec 2026-10-02 scaled-clip-sol,
operator yes 13:4xZ via Alpha).

The lane prefetches its USDC->SOL leg for the market's scale, so a coin classified at another scale
was refused `slot_lane_scaled_clip` before reserving (4 such fills went to the watcher, 09-27..10-01).
A SOL-funded clip uses no leg. With MEMECOIN_SLOT_LANE_SCALED_SOL on, a scaled pump buy reserves in
SOL when the native buffer can carry the scaled clip, and refuses as before when it cannot: never a
USDC reservation or a leg build on a scaled coin.
"""
import contextlib
import dataclasses
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_slot_lane as lane

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    with contextlib.closing(c):
        yield c


def _buy(**kw):
    base = dict(source_user_id="w1", owner="Owner1", mint="MintPump1", signature="Sig1",
                slot=450_000_000, sol_amount=2 * 10 ** 9,
                virtual_sol_reserves=30 * 10 ** 9, virtual_token_reserves=10 ** 9 * 10 ** 6,
                recv_utc="2026-09-26T20:30:00+00:00", recv_monotonic=time.monotonic())
    base.update(kw)
    return SimpleNamespace(**base)


def _lane(conn, tmp_path, monkeypatch, *, leg_scale, coin_scale, sol, shadow, knob=True):
    """A live lane: $100 fixed clip, SOL at $100, the leg prefetched for `leg_scale`, the coin
    classified at `coin_scale`. Returns (lane, record) where record holds every reserve_entry
    funding asset, every build (clip, funding_lamports) and every sent pending row."""
    # Source initiation is tested with real receipts in test_memecoin_solana_source_initiation.
    from sniper.services import memecoin_source_initiation as initiation
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    from sniper.execution import exec_lock
    from sniper.services import memecoin_market_state as ms
    monkeypatch.setattr(exec_lock, "_lock_path",
                        lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    for name, value in (("MEMECOIN_REAL_ENABLED", True), ("MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0),
                        ("MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0), ("MEMECOIN_REAL_SIZING", "fixed"),
                        ("MEMECOIN_REAL_SIZING_BY_CHAIN", {}), ("MEMECOIN_TRADE_SIZE_USD", 100.0),
                        ("MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 10_000.0)):
        monkeypatch.setattr(lane.cfg, name, value)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_SLOT_LANE_SCALED_SOL", knob, raising=False)
    real_classify = ms.classify_funded_entry
    monkeypatch.setattr(ms, "classify_funded_entry",
                        lambda *a, **k: dataclasses.replace(real_classify(*a, **k),
                                                            risk_scale=coin_scale))
    record = {"reserve": [], "built": [], "sent": []}
    real_reserve = risk.reserve_entry

    def reserve(*a, **k):
        record["reserve"].append(k.get("funding_asset", "usdc"))
        return real_reserve(*a, **k)
    monkeypatch.setattr(risk, "reserve_entry", reserve)

    def build_fn(buy, clip, _snap, *, funding_lamports=None):
        record["built"].append((clip, funding_lamports))
        if funding_lamports is None:
            return "USDC_LEG_MESSAGE", {}
        return "NATIVE_MESSAGE", {"funding_asset": "sol", "funding_amount_atomic": funding_lamports,
                                  "target_mint": buy.mint}

    def execute(_conn, pending, _message, **_kw):
        record["sent"].append(dict(pending))
        return {"status": "filled"}

    ln = lane.SlotLane(
        conn=conn, executor={"slot_execute_fn": execute}, build_fn=build_fn,
        snapshot_fn=lambda: lane.PrefetchSnapshot(
            sol_usd=100.0, usdc_balance=400.0, sol_balance=sol, blockhash="Hash1",
            last_valid_height=1,
            observed_mono={k: time.monotonic() for k in ("sol_usd", "balances", "blockhash")}),
        armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
        now_iso_fn=lambda: "2026-09-26T20:30:00+00:00", lock_key="sol-test", mode="live",
        shadow_clip_fn=lambda: shadow, sol_buffer_cap_usd=175.0, leg_scale_fn=lambda: leg_scale)
    return ln, record


def _pending_rows(conn):
    return conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0]


@pytest.mark.parametrize("leg_scale, coin_scale, shadow", [(1.0, 0.75, 100.0), (0.75, 1.0, 75.0)])
def test_a_scaled_coin_the_sol_buffer_covers_reserves_in_sol_and_builds_native(
        conn, tmp_path, monkeypatch, leg_scale, coin_scale, shadow):
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=leg_scale, coin_scale=coin_scale,
                    sol=2.0, shadow=shadow)
    result = ln.handle(_buy())
    assert result["status"] == "filled", result
    assert rec["reserve"] == ["sol"]
    (clip, lamports), = rec["built"]
    assert clip == pytest.approx(100.0 * coin_scale) and lamports > 0
    assert rec["sent"][0]["funding_asset"] == "sol"
    events = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts")
                        .fetchone()[0])["substage_events"]
    mark = next(e for e in events if e["name"] == "slot_scaled_sol")
    assert (mark["risk_scale"], mark["leg_scale"]) == (coin_scale, leg_scale)
    assert mark["clip_guess"] == pytest.approx(100.0 * coin_scale)


def test_a_scaled_coin_short_of_sol_refuses_and_never_reserves_usdc_or_builds_a_leg(
        conn, tmp_path, monkeypatch):
    """The pin Alpha asked for. A stale roster clip ($20) guesses $15, which the buffer (~$49)
    covers, so the SOL reservation is tried; it sizes the real $75 clip and refuses. The unscaled
    path would fall back to USDC there. A scaled coin must not: its leg does not fit."""
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=0.75, sol=0.5,
                    shadow=20.0)
    result = ln.handle(_buy())
    assert result["reason"] == lane.SCALED_CLIP_REASON
    assert rec["reserve"] == ["sol"] and rec["built"] == [] and rec["sent"] == []
    assert _pending_rows(conn) == 0


def test_with_the_knob_off_a_scaled_coin_refuses_before_any_reservation(conn, tmp_path, monkeypatch):
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=0.75, sol=2.0,
                    shadow=100.0, knob=False)
    assert ln.handle(_buy())["reason"] == lane.SCALED_CLIP_REASON
    assert rec == {"reserve": [], "built": [], "sent": []}


def test_a_scaled_coin_the_buffer_cannot_cover_refuses_before_any_reservation(
        conn, tmp_path, monkeypatch):
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=0.75, sol=0.05,
                    shadow=100.0)
    assert ln.handle(_buy())["reason"] == lane.SCALED_CLIP_REASON
    assert rec == {"reserve": [], "built": [], "sent": []}


@pytest.mark.parametrize("venue, extra", [("raydium_cpmm", {}),
                                          ("pump_amm", {"pool": "Pool1", "quote_mint": USDC_MINT})])
def test_a_scaled_coin_on_a_venue_without_native_funding_refuses(
        conn, tmp_path, monkeypatch, venue, extra):
    """CPMM has no native funding, and a USDC-quoted pump pool cannot be paid in SOL (E2)."""
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=0.75, sol=2.0,
                    shadow=100.0)
    facts = {"mint": "MintPump1", "price_usd": 3e-6, "mcap_usd": 3_000.0, "their_usd": 200.0}
    monkeypatch.setattr(lane, "admission_refusal", lambda *a, **k: (None, dict(facts)))
    monkeypatch.setattr(lane, "shadow_only", lambda _buy: False)
    monkeypatch.setattr(ln, "_venue_live_capped", lambda _buy: None)
    assert ln.handle(_buy(venue=venue, **extra))["reason"] == lane.SCALED_CLIP_REASON
    assert rec == {"reserve": [], "built": [], "sent": []}


@pytest.mark.parametrize("leg_scale, coin_scale", [(None, 0.75), (0.0, 0.75), (1.0, 1.5)])
def test_an_unreadable_scale_refuses_with_nothing_reserved(
        conn, tmp_path, monkeypatch, leg_scale, coin_scale):
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=leg_scale, coin_scale=coin_scale,
                    sol=2.0, shadow=100.0)
    assert ln.handle(_buy())["reason"] == lane.SCALED_CLIP_REASON
    assert rec == {"reserve": [], "built": [], "sent": []}


def test_an_unscaled_coin_still_falls_back_to_usdc_when_the_buffer_is_short(
        conn, tmp_path, monkeypatch):
    """Unchanged behaviour: the same stale guess, but the coin is at the leg's scale, so the leg
    fits and the USDC reservation is the fallback."""
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=1.0, sol=0.5,
                    shadow=20.0)
    assert ln.handle(_buy())["status"] == "filled"
    assert rec["reserve"] == ["sol", "usdc"] and rec["built"] == [(pytest.approx(100.0), None)]


def test_scaled_coin_uses_its_matching_ondemand_leg_without_native_sol(conn, tmp_path, monkeypatch):
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=0.75,
                    sol=0.05, shadow=100.0)
    facts = {'mint': 'MintPump1', 'price_usd': 3e-6, 'mcap_usd': 3000.0, 'their_usd': 200.0}
    monkeypatch.setattr(lane, 'admission_refusal', lambda *a, **k: (None, dict(facts)))
    monkeypatch.setattr(lane, 'shadow_only', lambda b: False)
    monkeypatch.setattr(ln, '_venue_live_capped', lambda b: None)
    monkeypatch.setattr(ln, '_handle_requeued', lambda b: ln._handle(b, hand_off=False))
    buy = _buy(venue='pump_amm', quote_mint=USDC_MINT,
               ondemand={'leg': {'usdc_atomic': 75_000_000}, 'quote': (6, 1, 0, 0)})
    result = ln.handle(buy)
    assert result['status'] == 'filled', result
    assert rec['reserve'] == ['usdc']
    assert rec['built'] == [(75.0, None)]


def test_scaled_coin_offers_its_clip_to_worker_before_reserving(conn, tmp_path, monkeypatch):
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=0.75,
                    sol=0.05, shadow=100.0)
    facts = {'mint': 'MintPump1', 'price_usd': 3e-6, 'mcap_usd': 3000.0, 'their_usd': 200.0}
    monkeypatch.setattr(lane, 'admission_refusal', lambda *a, **k: (None, dict(facts)))
    monkeypatch.setattr(lane, 'shadow_only', lambda b: False)
    monkeypatch.setattr(lane, 'exotic_live', lambda v: True)
    monkeypatch.setattr(ln, '_venue_live_capped', lambda b: None)
    offers=[]
    ln.exotic_offer=lambda buy, **kw: offers.append(kw) or True
    buy=_buy(venue='pump_amm', quote_mint=USDC_MINT)
    result=ln.handle(buy)
    assert result.get('exotic_pending') is True, result
    assert offers == [{'clip_usd': 75.0}]
    assert rec == {'reserve': [], 'built': [], 'sent': []}
    assert ln.pending_legs.holds(buy.signature, now=time.monotonic())


def _trial_lane(conn, tmp_path, monkeypatch):
    from sniper.services import memecoin_trial as T
    ln, rec = _lane(conn, tmp_path, monkeypatch, leg_scale=1.0, coin_scale=1.0,
                    sol=0.05, shadow=100.0)
    ln.pending_legs = lane.PendingLegs()
    tid = m.start_trial(conn, source_user_id='w1', chain='solana', clip_usd=20.0,
                        rule=T.RULE, actor='test', now_iso='2026-09-26T20:00:00+00:00')
    facts = {'mint': 'MintPump1', 'price_usd': 3e-6, 'mcap_usd': 3000.0, 'their_usd': 200.0}
    monkeypatch.setattr(lane, 'admission_refusal', lambda *a, **k: (None, dict(facts)))
    monkeypatch.setattr(lane, 'shadow_only', lambda b: False)
    monkeypatch.setattr(lane, 'exotic_live', lambda v: v == 'pump_amm')
    monkeypatch.setattr(ln, '_venue_live_capped', lambda b: None)
    return ln, rec, {'trial_id': tid, 'rule': T.RULE, 'clip_usd': 20.0}


def test_trial_short_of_sol_queues_its_cash_leg_before_reserving(conn, tmp_path, monkeypatch):
    ln, rec, marker = _trial_lane(conn, tmp_path, monkeypatch)
    offers = []
    ln.exotic_offer = lambda b, **kw: offers.append(kw) or True
    result = ln.handle(_buy(venue='pump_amm'))
    assert result.get('exotic_pending') is True, result
    assert offers == [{'clip_usd': 20.0, 'trial': marker}]
    assert rec == {'reserve': [], 'built': [], 'sent': []}


@pytest.mark.parametrize('trial_end', [None, 'stop', 'end'])
def test_queued_trial_cash_leg_keeps_its_marker_and_cannot_outlive_trial(
        conn, tmp_path, monkeypatch, trial_end):
    ln, rec, marker = _trial_lane(conn, tmp_path, monkeypatch)
    if trial_end == 'stop':
        m.stop_trial(conn, marker['trial_id'], now_iso='2026-09-26T20:29:00+00:00')
    elif trial_end == 'end':
        m.end_trial(conn, marker['trial_id'], now_iso='2026-09-26T20:29:00+00:00', reason='promoted')
    buy = _buy(venue='pump_amm', ondemand={'leg': {'usdc_atomic': 20_000_000},
               'quote': (9, 100.0, 0, 0), 'quote_mint': 'So11111111111111111111111111111111111111112',
               'requested_clip_usd': 20.0, 'trial': marker})
    ln.pending_legs.add(buy, deadline=time.monotonic() + 1)
    result = ln.handle(buy)
    if trial_end:
        assert result['reason'].startswith('trial_stopped' if trial_end == 'stop' else 'trial_ended'), result
        assert not rec['built'] and not rec['sent']
    else:
        assert result['status'] == 'filled', result
        assert rec['reserve'] == ['usdc'] and rec['built'] == [(20.0, None)]
        assert json.loads(rec['sent'][0]['funded_contract_json'])['source']['trial'] == marker
        assert ln.handle(buy)['reason'] == lane.EXOTIC_AGED_OUT_REASON
        assert len(rec['sent']) == 1


@pytest.mark.parametrize('atoms', [100_000_000, 1_000_000, None, float('nan')])
def test_trial_never_spends_a_wrong_or_unreadable_cash_leg(conn, tmp_path, monkeypatch, atoms):
    ln, rec, marker = _trial_lane(conn, tmp_path, monkeypatch)
    buy = _buy(venue='pump_amm', ondemand={'leg': {'usdc_atomic': atoms}, 'quote': (9, 100, 0, 0),
               'requested_clip_usd': 20.0, 'trial': marker})
    ln.pending_legs.add(buy, deadline=time.monotonic() + 1)
    assert ln.handle(buy)['reason'].startswith('trial_unmarked')
    assert rec == {'reserve': [], 'built': [], 'sent': []}


def test_full_trial_leg_worker_releases_its_hold_for_the_single_fallback(conn, tmp_path, monkeypatch):
    ln, rec, marker = _trial_lane(conn, tmp_path, monkeypatch)
    ln.exotic_offer = lambda b, **kw: False
    buy = _buy(venue='pump_amm')
    assert ln.handle(buy)['reason'].startswith('trial_unmarked')
    assert not ln.pending_legs.holds(buy.signature, now=time.monotonic())
    assert rec == {'reserve': [], 'built': [], 'sent': []}


def test_trial_native_reservation_shortfall_queues_cash_without_signing(conn, tmp_path, monkeypatch):
    ln, rec, marker = _trial_lane(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_GAS_ENTRY_SOL', 0.0001)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_GAS_EXIT_SOL', 0.0001)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_GAS_RETRY_FACTOR', 1.0)
    snapshot = dataclasses.replace(ln.snapshot_fn(), sol_balance=0.205)
    ln.snapshot_fn = lambda: snapshot
    offers = []
    ln.exotic_offer = lambda b, **kw: offers.append(kw) or True
    result = ln.handle(_buy(venue='pump_amm'))
    assert result.get('exotic_pending') is True, result
    assert rec == {'reserve': ['sol'], 'built': [], 'sent': []}
    assert offers == [{'clip_usd': 20.0, 'trial': marker}]
    assert _pending_rows(conn) == 0

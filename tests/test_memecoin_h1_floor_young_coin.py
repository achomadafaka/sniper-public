"""The hourly volume floor on a coin younger than an hour (operator order 2026-09-25 ~21:45Z).

CLAIMS (Solana, attempt caaa6a99, 14:02Z) was 85 s old. DexScreener's h1 and m5 were the same
$2,339.24 because every trade the coin had ever made was inside the last five minutes. After the
source's own $222 was subtracted, $2,117.24 was judged against a $3,000 HOURLY floor and refused,
labelled "revival guard". An hour the coin has not lived cannot be held to an hour's volume: the
floor is pro-rated by the pair's age, never below five minutes' worth.
"""
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg
from test_memecoin_translation_gates import _EvObj, _classification_context, _conn, _no_chase

NOW = "2026-07-15T13:30:01Z"
_NOW_MS = datetime(2026, 7, 15, 13, 30, 1, tzinfo=timezone.utc).timestamp() * 1000.0


def _flat_floor_only(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 3_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 1_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_TURNOVER_M5", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TXNS_M5", 0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", True)


def _hook(monkeypatch, *, age_s, volume=2_339.24, token_age_s=None):
    c = _conn()
    _no_chase(monkeypatch)
    _flat_floor_only(monkeypatch)
    reached = {}
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: reached.setdefault("hit", True) and
                        {"status": "rejected", "reason": "executor seam"})
    ev = _EvObj()
    ev.their_size_usd = 222.0
    stats = {"price": 5.0, "market_cap": 5_634.99, "volume_h1": volume, "volume_m5": volume}
    if age_s is not None:
        stats["pair_created_at"] = _NOW_MS - age_s * 1000.0
    if token_age_s is not None:
        stats["token_age_s"] = token_age_s
    res = R.real_entry_hook(
        c, ev, {}, now_fn=lambda: NOW, trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(5_634.99),
        stats_fn=lambda mint, network_id=None: dict(stats))
    return res, reached


def test_claims_85_seconds_old_is_not_held_to_an_hour_of_volume(monkeypatch):
    res, reached = _hook(monkeypatch, age_s=85.19)
    assert reached.get("hit"), res           # passed the volume gate and reached the executor


def test_the_same_coin_an_hour_old_still_faces_the_whole_floor(monkeypatch):
    res, reached = _hook(monkeypatch, age_s=4_000.0)
    assert res["status"] == "rejected" and not reached
    assert res["reason"] == "h1 organic volume $2117 under floor $3000 (under_abs_floor)"


def test_a_young_pair_on_an_old_token_faces_the_whole_floor(monkeypatch):
    """Alpha's review of 3c46d15f: a migrated coin (curve -> PumpSwap, V2 -> V3, a relaunch pair)
    has a young pair on an old token. The OLDEST known age decides, so a 90 s pair on a three-day
    token is held to the whole hour."""
    res, reached = _hook(monkeypatch, age_s=90.0, token_age_s=3 * 86_400.0)
    assert res["status"] == "rejected" and not reached
    assert res["reason"] == "h1 organic volume $2117 under floor $3000 (under_abs_floor)"


def test_a_young_coin_under_its_pro_rated_floor_says_so(monkeypatch):
    """Manlet, Solana, 2026-09-23: 90 s old, $0.02 of volume. Still refused, against the bar it
    was actually held to."""
    res, reached = _hook(monkeypatch, age_s=90.0, volume=0.02)
    assert not reached
    assert res["reason"] == ("h1 organic volume $0 under floor $250 "
                             "(under_abs_floor, pro-rated to pair age 90s)")


def test_an_unknown_age_keeps_todays_rule_and_says_nothing_about_revival(monkeypatch):
    res, reached = _hook(monkeypatch, age_s=None)
    assert res["status"] == "rejected" and not reached
    assert "revival" not in res["reason"]


@pytest.mark.parametrize("age_s,organic,need,allowed", [
    (85.19, 2_117.24, 250.0, True),        # CLAIMS: five minutes is the least age credited
    (1_800.0, 1_400.0, 1_500.0, False),    # half an hour old: half the floor
    (1_800.0, 1_600.0, 1_500.0, True),
    (3_600.0, 2_999.0, 3_000.0, False),    # an hour old: the whole floor
    (None, 2_999.0, 3_000.0, False),       # unknown age: today's rule
    (-5.0, 2_999.0, 3_000.0, False),       # a clock that disagrees is not an age
])
def test_the_flat_floor_is_pro_rated_by_pair_age(age_s, organic, need, allowed):
    ok, bar, why = R.volume_alive_decision(organic, None, abs_floor=3_000.0, turnover_floor=0.0,
                                           pair_age_s=age_s)
    assert (ok, bar) == (allowed, pytest.approx(need))
    assert why == ("ok" if allowed else "under_abs_floor")


def test_a_refusal_claims_a_revival_only_when_the_revival_watch_is_armed(monkeypatch):
    from sniper.services import memecoin_revival_watch as rw
    monkeypatch.setattr(rw, "WATCHES", {})
    monkeypatch.setattr(rw, "_SPENT", {})
    c = _conn()
    _no_chase(monkeypatch)
    _flat_floor_only(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 0.0)
    monkeypatch.setattr(cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.10)
    monkeypatch.setattr(cfg, "micro_min_volume_m5_usd_for", lambda *a: 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REVIVAL_WATCH_MAX_MCAP_USD", 0.0, raising=False)
    ev = _EvObj()
    ev.their_size_usd = 10.0
    run = lambda: R.real_entry_hook(
        c, ev, {}, now_fn=lambda: NOW, trade_size=50.0, max_age_s=3600.0,
        classification_context=_classification_context(50_000.0),
        stats_fn=lambda mint, network_id=None: {"price": 5.0, "market_cap": 50_000.0,
                                                "volume_h1": 20_000.0, "volume_m5": 100.0})
    monkeypatch.setattr(cfg, "MEMECOIN_REVIVAL_WATCH_ENABLED", False, raising=False)
    assert run()["reason"].endswith("(m5_turnover_under_floor)")
    monkeypatch.setattr(cfg, "MEMECOIN_REVIVAL_WATCH_ENABLED", True, raising=False)
    reason = run()["reason"]
    assert reason.endswith("(m5_turnover_under_floor, revival watch armed)")
    from sniper.services.memecoin_refusal_capture import GATES
    assert dict(GATES)["m5_turnover_under_floor"](reason)     # still read as its gate


@pytest.mark.parametrize("age,token_age,volume,allowed", [
    (0, None, 600, True), (90, None, 600, True), (None, None, 600, False),
    (90, 7200, 600, False), (1800, None, 1500, True), (1800, None, 1499, False)])
def test_revival_uses_same_oldest_age_floor(monkeypatch, age, token_age, volume, allowed):
    from sniper.services import memecoin_volume_writer as vw
    _flat_floor_only(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", False)
    monkeypatch.setattr(cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.1)
    monkeypatch.setattr(cfg, "inband_max_turnover_m5_for", lambda *a: 0.0)
    monkeypatch.setattr(cfg, "micro_min_volume_m5_usd_for", lambda *a: 0.0)
    monkeypatch.setattr(vw, "gate_evidence", lambda *a, **kw: None)
    stats = {"volume_h1": volume, "volume_m5": 100, "token_age_s": token_age}
    if age is not None:
        stats["pair_created_at"] = _NOW_MS - age * 1000
    ev = _EvObj()
    kw = dict(mcap=5000, network_id="solana", coin_band="nano", decision_at=NOW)
    assert not R.revival_watch_volume_cleared(stats, ev, **kw)
    stats["volume_m5"] = volume
    assert R.revival_watch_volume_cleared(stats, ev, **kw) is allowed


def test_an_h1_refusal_prints_the_tape_bound_it_decided_on(monkeypatch):
    """c88c1b88 (4663, 2026-09-27): the gate decided on max(provider $1, tape m5 $1,698) and the
    reason printed "$1". The reason names the figure the gate actually held to the floor."""
    from sniper.services import memecoin_volume_writer as V
    monkeypatch.setattr(V, "gate_evidence", lambda *a, **k: {
        "reason": "ok", "usd": 1698.8, "unverified_swaps": 0, "swaps": 30, "pools": [],
        "unpriced_pools": [], "excluded_tx_seen": True, "coverage": "partial"})
    res, reached = _hook(monkeypatch, age_s=4_000.0, volume=223.0)
    assert res["status"] == "rejected" and not reached
    assert res["reason"] == "h1 organic volume $1699 under floor $3000 (under_abs_floor)"

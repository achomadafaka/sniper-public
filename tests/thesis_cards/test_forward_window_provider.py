"""Forward-window provider (calibration_edge) — tested against a fake fetcher (no network).

Proves: None only on fetch failure; [] when nothing qualifies; strict PIT (post-since);
resolved-only; negativeRisk excluded (flag + condition-set); correct sizing-row shape;
dead_zone/unknown metrics -> None; and end-to-end through run_forward_tracking.
"""
from __future__ import annotations

import pytest

from sniper.analysis.thesis_cards.enrollment import InMemoryPaperEnrollmentStore, PaperEnrollment
from sniper.analysis.thesis_cards.forward_tracking import run_forward_tracking
from sniper.analysis.thesis_cards.forward_window_provider import WalletForwardWindowProvider

_SINCE = 1000


def raw(avg=0.45, cur=1.0, ts=2000, rpnl=55.0, cid="0xc", **extra):
    return {"avgPrice": avg, "curPrice": cur, "timestamp": ts, "realizedPnl": rpnl,
            "conditionId": cid, **extra}


def _provider(rows, **kw):
    return WalletForwardWindowProvider(lambda w: rows, **kw)


def test_none_only_when_fetch_fails():
    prov = WalletForwardWindowProvider(lambda w: None)
    assert prov("0xw", _SINCE, "calibration_edge") is None


def test_empty_list_when_nothing_qualifies():
    prov = _provider([raw(ts=500)])                       # all pre-enrollment
    assert prov("0xw", _SINCE, "calibration_edge") == []


def test_maps_resolved_winner_to_sizing_row():
    prov = _provider([raw(avg=0.45, cur=1.0, ts=2000, rpnl=55.0)])
    win = prov("0xw", _SINCE, "calibration_edge")
    assert len(win) == 1
    r = win[0]
    assert set(r) == {"entry_price", "size", "won"}
    assert r["entry_price"] == 0.45 and r["won"] == 1
    assert r["size"] == pytest.approx(45.0)               # |55/(1-0.45)| * 0.45


def test_maps_resolved_loser():
    prov = _provider([raw(avg=0.45, cur=0.0, ts=2000, rpnl=-45.0)])
    r = prov("0xw", _SINCE, "calibration_edge")[0]
    assert r["won"] == 0 and r["size"] == pytest.approx(45.0)


def test_excludes_pre_enrollment_rows():
    prov = _provider([raw(ts=500), raw(ts=2000)])
    assert len(prov("0xw", _SINCE, "calibration_edge")) == 1


def test_excludes_unresolved_rows():
    prov = _provider([raw(cur=0.55), raw(cur=1.0)])        # 0.55 is mid -> unresolved
    assert len(prov("0xw", _SINCE, "calibration_edge")) == 1


def test_excludes_negative_risk_by_flag():
    prov = _provider([raw(negRisk=True), raw()])
    assert len(prov("0xw", _SINCE, "calibration_edge")) == 1


def test_excludes_negative_risk_by_condition_set():
    prov = _provider([raw(cid="0xBAD"), raw(cid="0xOK")], negrisk_conditions={"0xBAD"})
    win = prov("0xw", _SINCE, "calibration_edge")
    assert [r for r in win] and all(True for r in win)
    assert len(win) == 1


def test_dead_zone_and_unknown_metric_return_none():
    prov = _provider([raw()])
    assert prov("0xw", _SINCE, "dead_zone_drift_gap") is None
    assert prov("0xw", _SINCE, "totally_unknown") is None


def test_end_to_end_provider_feeds_d4():
    # 5 qualifying rows -> sizing_card underpowered (n<20) -> D4 awaiting_data, row stays active
    store = InMemoryPaperEnrollmentStore()
    ftd = {"metric": "calibration_edge", "horizon": "to_resolution", "success_threshold": 0.0,
           "kill_threshold": -0.05, "min_forward_n": 20, "stake_convention": "uniform_paper_unit"}
    pe = PaperEnrollment(wallet="0xw", card_id="sizing_card", card_version="1",
                         profile_run_id="r1", rank=1, q_value=0.01, evidence_state="supported",
                         data_tier=None, forward_test_design=ftd, state="active",
                         enrolled_effect=0.10, enrolled_at=_SINCE)
    store.records[pe.idempotency_key] = pe
    prov = _provider([raw(ts=2000 + i) for i in range(5)])
    out = run_forward_tracking(store, prov, now=_SINCE + 86400)
    rec = store.records["0xw|sizing_card|1|r1"]
    assert rec.state == "active" and rec.forward_metric["status"] == "awaiting_data"
    assert rec.forward_metric["forward_n"] == 5

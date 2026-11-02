"""stage_classify prefers the CSE verdict when a MarketContext is supplied.

Covers the classify→route supersession: with a point-in-time context + raw fills,
the pipeline uses the Counterfactual Selection Engine (which gates 'algorithmic'
on a real selection edge); without one it falls back to the behavioral heuristic;
a broken context falls back too, recording the error rather than swallowing it.
"""
from __future__ import annotations

import json

from sniper.analysis.algo_discovery.classifier import cse_classification
from sniper.analysis.algo_discovery.pipeline import Ctx, stage_classify


def _planted_edge_context(tmp_path):
    """A universe where cheap underdogs out-resolve expensive ones (planted edge),
    saved as a build_cse_pit_index-shaped JSON the loader restores."""
    meta, pidx = {}, {}
    for i in range(60):
        cid = f"m{i}"
        cheap = i < 30
        price = (0.20 + 0.005 * (i % 15)) if cheap else (0.45 + 0.005 * ((i - 30) % 15))
        resolved = (1 if i < 21 else 0) if cheap else (1 if i < 41 else 0)
        meta[cid] = {"created_at": 1000, "end_date": 1_000_000, "category": "sports",
                     "market_type": "binary", "outcome_role": "underdog",
                     "favorite_rank": 2, "resolved_to_side": resolved}
        pidx[cid] = [[1000 + 100 * k, price] for k in range(60)]
    p = tmp_path / "ctx.json"
    p.write_text(json.dumps({"market_meta": meta, "price_index": pidx}))
    return p


def _wallet_data_picking_cheap():
    """activity_rows where the wallet selects the 30 cheap underdogs."""
    rows = []
    for i in range(30):
        cid = f"m{i}"
        ts = 2000 + i * 120
        for k in range(4):
            rows.append({"type": "TRADE", "side": "BUY", "conditionId": cid,
                         "asset": f"a{i}", "price": 0.25, "size": 10,
                         "timestamp": ts + k * 5, "outcome": "YES",
                         "market_category": "sports"})
    return {"wallet": "0xcse", "activity_rows": rows}


def _dossier_tables(wallet_data):
    from sniper.analysis.wallet_dossier.builder import build_tables
    import dataclasses
    # minimal protocol_report — builder tolerates sparse input
    return dataclasses.asdict(build_tables(wallet="0xcse", wallet_data=wallet_data,
                                           protocol_report={}))


def test_cse_supersedes_when_context_supplied(tmp_path):
    ctx_path = _planted_edge_context(tmp_path)
    wd = _wallet_data_picking_cheap()
    out = stage_classify({}, Ctx(wallet="0xcse", cse_context_path=ctx_path),
                         wallet_data=wd)
    assert out["classifier_version"] == "cse_counterfactual_selection_v1"
    assert out["label"] == "algorithmic"
    assert out["route_hint"] == "rebuild_rule"
    assert out["signals"]["cse_classification"] == "algorithmic_self_contained"
    assert out["signals"]["selection_edge"]["supported"] is True
    # never claims certainty
    assert out["confidence"] <= 0.8
    assert out["signals"]["honest_caveat"]


def test_falls_back_to_behavioral_without_context():
    wd = _wallet_data_picking_cheap()
    out = stage_classify({}, Ctx(wallet="0xcse", cse_context_path=None), wallet_data=wd)
    assert out["classifier_version"] == "algo_discovery_behavior_classifier_v1"


def test_broken_context_falls_back_and_records_error(tmp_path):
    bad = tmp_path / "missing.json"
    wd = _wallet_data_picking_cheap()
    out = stage_classify({}, Ctx(wallet="0xcse", cse_context_path=bad), wallet_data=wd)
    assert out["classifier_version"] == "algo_discovery_behavior_classifier_v1"
    assert out["signals"]["cse_attempted"] is True
    assert "cse_error" in out["signals"]


def test_cse_classification_forwards_closed_positions(monkeypatch):
    seen = {}

    def fake_runner(activity, context, **kwargs):
        seen["closed_positions"] = kwargs.get("closed_positions")
        return {
            "verdict": {
                "classification": "undetermined",
                "route": "collect_more",
                "confidence": 0.4,
                "dimensions": {},
                "qualitative_note": "",
                "honest_caveat": "",
            },
            "rule": {},
            "selection_edge": {},
            "candidate_quality": {},
            "decision_signals": {},
        }

    monkeypatch.setattr(
        "sniper.analysis.counterfactual_selection.runner.run_counterfactual_selection",
        fake_runner,
    )
    closed = [{"asset": "a0", "outcomeIndex": 1, "eventSlug": "event-a"}]
    cse_classification(
        {"activity_rows": [], "closed_positions": closed},
        context=object(),
    )

    assert seen["closed_positions"] == closed

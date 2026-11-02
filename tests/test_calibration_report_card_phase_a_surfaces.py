"""Calibration report-card visibility for Phase A audit surfaces."""
from __future__ import annotations

import json


def _fake_report() -> dict:
    return {
        "protocol_version": "v2",
        "overall_verdict": "data_insufficient",
        "primary_edge_type": None,
        "copyability_note": None,
        "gate_result": "pass",
        "gate_reason": None,
        "trading_mode": "n/a_routing",
        "flip_rate": None,
        "archetype_layer1": "single_sided_specialist",
        "archetype_layer2": "news_reactor",
        "n_closed_positions": 3,
        "n_held_to_resolution": 2,
        "n_open_positions": 0,
        "open_position_breakdown": {
            "n_open_in_money": 0,
            "in_money_capital_usdc": 0.0,
            "n_open_underwater": 0,
            "underwater_capital_usdc": 0.0,
            "n_open_dead": 0,
            "dead_capital_usdc": 0.0,
        },
        "activity_rows_fetched": 10,
        "walk_full_history": True,
        "activity_cap_hit": False,
        "subgraph_fallback_used": False,
        "n_unmapped_fills": 0,
        "realized_pnl": 12.0,
        "unrealized_pnl": 0.0,
        "combined_pnl": 12.0,
        "net_pnl": 12.0,
        "realized_roi": 0.10,
        "unrealized_roi": None,
        "combined_roi": 0.10,
        "dw_roi_point": 0.10,
        "dw_roi_ci_lower": 0.05,
        "dw_roi_ci_upper": 0.15,
        "ew_roi_point": 0.08,
        "time_coverage": {
            "first_position_ts": None,
            "last_position_ts": None,
            "first_fill_ts": None,
            "last_fill_ts": None,
            "duration_days": None,
            "n_positions_per_month": 0.0,
            "fetch_termination": {"closed_positions": "complete"},
        },
        "held_analyst_branch": None,
        "flipper_branch": None,
        "data_quality_flags": [
            "ANALYST_FAST_MODE_OMITTED:price_history,exec_attribution,capital_ledger"
        ],
        "applicable_limitations": [],
        "capital_ledger": {},
        "outcome_side_breakdown": {
            "yes": {"n": 3, "win_rate": 1.0, "total_pnl": 12.0, "share_of_closed": 1.0},
            "no": {"n": 0, "win_rate": None, "total_pnl": 0.0, "share_of_closed": 0.0},
            "category_clusters": [
                {"category": "Politics", "n": 3, "pnl": 12.0, "total_pnl": 12.0}
            ],
        },
        "timing_surface": {"classification": "unavailable"},
        "exit_attribution": {"coverage": {"n_total": 3}},
        "position_building_surface": {},
        "extractor_features_summary": {},
        "data_completeness": {
            "closed_positions_capture": {
                "closed_positions_fetched": 3,
                "closed_positions_cap_hit": False,
            },
            "tag_lookup_state": "ok",
        },
        "triage_context": {"source": "leaderboard", "period": "MONTH"},
    }


def test_report_card_prints_phase_a_surfaces(monkeypatch, tmp_path, capsys):
    import scripts.calibration_report_card as card

    wallet = "0xabc"
    fixture_dir = tmp_path / "tests" / "calibration" / "fixtures"
    fixture_dir.mkdir(parents=True)
    fixture = fixture_dir / f"{wallet}_2026-05-17.json"
    fixture.write_text(json.dumps({"schema_version": "fixture-v3"}))

    monkeypatch.setattr(card, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(card, "FIXTURE_DIR", fixture_dir)
    monkeypatch.setattr(card, "_git_rev", lambda: "abc123")
    monkeypatch.setattr(card, "_git_dirty", lambda: "clean")
    monkeypatch.setattr(card, "_replay", lambda w, f: _fake_report())

    rc = card.main(["--fixture", str(fixture)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "--- TRIAGE CONTEXT ---" in out
    assert "source: leaderboard" in out
    assert "--- CATEGORY CLUSTERS ---" in out
    assert "Politics" in out
    assert "closed_positions_capture" in out
    assert "tag_lookup_state: ok" in out
    assert "ANALYST_FAST_MODE_OMITTED" in out

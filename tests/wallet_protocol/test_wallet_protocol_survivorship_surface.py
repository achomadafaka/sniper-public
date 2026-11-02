"""Acceptance tests — Survivorship surface (`survivorship_surface`).

Spec: docs/superpowers/specs/2026-05-22-survivorship-surface-design.md

Display-only surface: flags when a wallet was discovered via a
winner-ranked list (survivorship bias). Pure function of triage_context.
Hard wall identical to the sibling surfaces — never feeds verdict / gate /
primary_edge_type / copyability_note / data_quality_flags.

RED until `compute_survivorship_surface` exists and is wired into
`analyze_wallet` + `ProtocolReport.survivorship_surface`.
"""
from __future__ import annotations

import json
import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.survivorship import (
    compute_survivorship_surface,
)
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


def _data() -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=_now(),
        replay_surface_meta={
            "archetype_layer1": {"status": "skipped"},
            "execution_profile": {"status": "skipped"},
            "price_history_lookups": {"status": "skipped"},
            "leafs_by_cid": {"status": "skipped"},
            "nav_lookup": {"status": "skipped"},
            "peer_baselines": {"status": "skipped"},
        },
    )


def _signature(report: ProtocolReport) -> tuple:
    return (
        report.overall_verdict,
        report.gate_result,
        report.primary_edge_type,
        report.copyability_note,
        tuple(report.data_quality_flags),
    )


# ==========================================================================
# §8 — acceptance tests
# ==========================================================================
def test_leaderboard_source_is_flagged():
    """#1 — a leaderboard discovery source → survivor_sampled True, the
    source echoed, a caveat that names the source."""
    s = compute_survivorship_surface(
        {"source": "polymarket_public_leaderboard"})
    assert s["survivor_sampled"] is True
    assert s["discovery_source"] == "polymarket_public_leaderboard"
    assert s["caveat"] and "polymarket_public_leaderboard" in s["caveat"]


def test_other_winner_list_markers_flagged():
    """#2 — top_pnl / winner / top_wallet each trip survivor_sampled."""
    for src in ("daily_top_pnl", "weekly_winner_board", "top_wallet_scan"):
        s = compute_survivorship_surface({"source": src})
        assert s["survivor_sampled"] is True, src


def test_no_triage_context_is_unknown():
    """#3 — no triage context → survivor_sampled None, source None,
    caveat None (status unknown, never asserted clean)."""
    s = compute_survivorship_surface(None)
    assert s["survivor_sampled"] is None
    assert s["discovery_source"] is None
    assert s["caveat"] is None


def test_unrecognized_source_is_unknown_not_false():
    """#4 — a source that is not a known winner-list → survivor_sampled
    None (NEVER False — neutrality cannot be confirmed), caveat None, the
    source still echoed."""
    s = compute_survivorship_surface({"source": "manual_add"})
    assert s["survivor_sampled"] is None
    assert s["caveat"] is None
    assert s["discovery_source"] == "manual_add"


def test_note_always_present():
    """#5 — the structural note is non-empty in every case."""
    for tc in (None, {"source": "polymarket_public_leaderboard"},
               {"source": "manual_add"}, {}):
        s = compute_survivorship_surface(tc)
        assert isinstance(s["note"], str) and s["note"]


def test_hard_wall_surface_never_mutates_verdict(monkeypatch):
    """#6 — a garbage-stub producer leaves verdict / gate / primary_edge_type
    / copyability_note / data_quality_flags byte-identical."""
    data = _data()
    tc = {"source": "polymarket_public_leaderboard"}
    baseline = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                              seed=1, triage_context=tc)

    import sniper.analysis.wallet_protocol.survivorship as sv

    monkeypatch.setattr(
        sv, "compute_survivorship_surface",
        lambda *a, **k: {"survivor_sampled": True, "GARBAGE": 9.99e9},
    )
    poisoned = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=100,
                              seed=1, triage_context=tc)
    assert poisoned.survivorship_surface.get("GARBAGE") == 9.99e9
    assert _signature(poisoned) == _signature(baseline)
    assert "GARBAGE" not in str(poisoned.data_quality_flags)


def test_field_present_on_live_path():
    """#7 — survivorship_surface is a dict on the analyze_wallet path."""
    report = analyze_wallet(
        "0xabc", pre_fetched_data=_data(), n_iter=100, seed=1,
        triage_context={"source": "polymarket_public_leaderboard"})
    assert isinstance(report.survivorship_surface, dict)
    assert report.survivorship_surface["survivor_sampled"] is True


def test_replay_determinism():
    """#8 — same input → byte-identical surface JSON across two runs."""
    tc = {"source": "polymarket_public_leaderboard"}
    a = compute_survivorship_surface(tc)
    b = compute_survivorship_surface(tc)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_backward_compat_protocolreport_without_field():
    """#9 — a ProtocolReport built without the field defaults to None."""
    rep = ProtocolReport(wallet="0xabc")
    assert rep.survivorship_surface is None

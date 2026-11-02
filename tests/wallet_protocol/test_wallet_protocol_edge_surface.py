"""Edge Surface v1 — descriptive performance surfaces.

Hard invariant: these surfaces are display-only — they must NEVER alter
overall_verdict / gate_result / primary_edge_type / copyability_note
compared to a matched WalletData with the new fields cleared.

Coverage:
- Per-grain financials (held branch + flipper branch).
- Outcome-side (YES / NO / OTHER) breakdown.
- Extractor features summary passthrough.
- Edge surface section omitted when nothing populated.
- Verdict invariance assertions.
"""
from __future__ import annotations

import time

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import (
    _compute_outcome_side_breakdown,
    analyze_wallet,
)
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


def _pos(
    cid="cid_a", outcome="Yes", curPrice=1.0, avgPrice=0.50, size=100,
    realizedPnl=50.0, ts=None, eventSlug=None,
):
    if ts is None:
        ts = _now()
    elif ts < 1_000_000_000:
        ts = _now() - 86400 + ts
    return {
        "conditionId": cid,
        "outcome": outcome,
        "curPrice": curPrice,
        "avgPrice": avgPrice,
        "size": size,
        "realizedPnl": realizedPnl,
        "timestamp": ts,
        "eventSlug": eventSlug or f"slug-{cid}",
    }


def _wallet_data(**overrides) -> WalletData:
    base = dict(
        wallet="0xabc",
        closed_positions=[_pos(realizedPnl=50.0, ts=1000 + i) for i in range(40)],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )
    base.update(overrides)
    return WalletData(**base)


# ---------------------------------------------------------------------------
# Outcome-side breakdown
# ---------------------------------------------------------------------------


def test_outcome_side_breakdown_buckets_yes_no_other():
    """Outcome-side breakdown counts a position as a win iff
    realizedPnl > 0. This handles both resolution wins and pre-resolution
    profitable exits uniformly.

    Layout: 6 YES wins + 4 YES losses, 3 NO wins + 7 NO losses, 2 OTHER wins.
    """
    closed = []
    closed += [_pos(outcome="Yes", realizedPnl=50.0, ts=1000 + i) for i in range(6)]
    closed += [_pos(outcome="Yes", realizedPnl=-50.0, ts=1010 + i) for i in range(4)]
    closed += [_pos(outcome="No", realizedPnl=50.0, ts=1020 + i) for i in range(3)]
    closed += [_pos(outcome="No", realizedPnl=-50.0, ts=1030 + i) for i in range(7)]
    closed += [_pos(outcome="Trump", realizedPnl=50.0, ts=1040 + i) for i in range(2)]
    osb = _compute_outcome_side_breakdown(closed)
    assert osb is not None
    assert osb["yes"]["n"] == 10
    assert osb["yes"]["win_rate"] == pytest.approx(0.6)
    assert osb["yes"]["total_pnl"] == pytest.approx(50.0 * 6 + -50.0 * 4)
    assert osb["no"]["n"] == 10
    assert osb["no"]["win_rate"] == pytest.approx(0.3)
    assert osb["no"]["total_pnl"] == pytest.approx(50.0 * 3 + -50.0 * 7)
    assert "other" in osb
    assert osb["other"]["n"] == 2
    assert osb["other"]["win_rate"] == pytest.approx(1.0)


def test_outcome_side_breakdown_zero_pnl_position_counts_as_loss():
    """Edge case: realizedPnl exactly 0 is not a strict win (>0)."""
    closed = [_pos(outcome="Yes", realizedPnl=0.0, ts=1000)]
    osb = _compute_outcome_side_breakdown(closed)
    assert osb["yes"]["n"] == 1
    assert osb["yes"]["win_rate"] == 0.0


def test_other_bucket_detail_outcome_labels_and_top_markets():
    """OTHER bucket exposes outcome_labels (top by count), top_markets_by_pnl,
    top_markets_by_volume, and tag_coverage."""
    closed = []
    # 9 'Under' bets — 2 winners $300, 7 losers $-50
    for i in range(2):
        closed.append({**_pos(outcome="Under", realizedPnl=300.0,
                              cid=f"cid_under_w_{i}", ts=1000 + i),
                       "title": f"Match A — total goals over/under",
                       "totalBought": 1000.0})
    for i in range(7):
        closed.append({**_pos(outcome="Under", realizedPnl=-50.0,
                              cid=f"cid_under_l_{i}", ts=1100 + i),
                       "title": f"Match B — total goals over/under",
                       "totalBought": 100.0})
    # 6 Trump bets — 4 wins, 2 losses, with one big PnL
    for i in range(4):
        closed.append({**_pos(outcome="Trump", realizedPnl=200.0,
                              cid=f"cid_trump_w_{i}", ts=2000 + i),
                       "title": "2024 election winner", "totalBought": 800.0})
    closed.append({**_pos(outcome="Trump", realizedPnl=10000.0,
                          cid="cid_trump_big", ts=2100),
                   "title": "Will Trump win Iowa?",
                   "totalBought": 50000.0})
    closed.append({**_pos(outcome="Trump", realizedPnl=-100.0,
                          cid="cid_trump_l", ts=2200),
                   "title": "Random Trump market", "totalBought": 200.0})

    osb = _compute_outcome_side_breakdown(closed)
    assert osb is not None
    other = osb["other"]
    # 9 Under + 6 Trump = 15.
    assert other["n"] == 15
    assert other["share_of_closed"] == pytest.approx(1.0)
    # Outcome labels: Under=9, Trump=6
    label_names = [e["label"] for e in other["outcome_labels"]]
    assert label_names[:2] == ["Under", "Trump"]
    # Top markets by P&L: the $10K Trump win should lead.
    top_pnl = other["top_markets_by_pnl"]
    assert top_pnl[0]["pnl"] == pytest.approx(10000.0)
    assert top_pnl[0]["title"] == "Will Trump win Iowa?"
    assert top_pnl[0]["outcome"] == "Trump"
    # Top markets by volume: the $50K totalBought row should lead.
    top_vol = other["top_markets_by_volume"]
    assert top_vol[0]["total_bought"] == pytest.approx(50000.0)
    # Tag coverage: leafs_by_cid was None, so coverage reports "not available".
    cov = other["tag_coverage"]
    assert cov["with_leaf_tags"] is None
    assert cov["total"] == 15


def test_other_bucket_tag_coverage_with_leafs_and_nav():
    closed = [
        {**_pos(outcome="Trump", realizedPnl=200.0, cid="cid_a", ts=1000),
         "title": "Will Trump win Iowa?", "totalBought": 500.0},
        {**_pos(outcome="Kamala", realizedPnl=-100.0, cid="cid_b", ts=1100),
         "title": "Will Kamala win MI?", "totalBought": 300.0},
        {**_pos(outcome="England", realizedPnl=50.0, cid="cid_c", ts=1200),
         "title": "England vs Italy", "totalBought": 100.0},
        # cid_d has no leaf tags in lookup → without_leaf_tags
        {**_pos(outcome="Other", realizedPnl=10.0, cid="cid_d", ts=1300),
         "title": "Untagged market", "totalBought": 50.0},
    ]
    leafs_by_cid = {
        "cid_a": ["politics", "us-elections"],
        "cid_b": ["politics", "us-elections"],
        "cid_c": ["sports", "soccer"],
        "cid_d": [],  # explicitly empty → without_leaf_tags
    }
    nav_lookup = {
        "politics": "politics", "us-elections": "elections",
        "sports": "sports", "soccer": "sports",
    }
    osb = _compute_outcome_side_breakdown(
        closed, leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
    )
    other = osb["other"]
    cov = other["tag_coverage"]
    assert cov["total"] == 4
    assert cov["with_leaf_tags"] == 3
    assert cov["without_leaf_tags"] == 1
    assert cov["with_nav_mapping"] == 3
    assert cov["without_nav_mapping"] == 0
    nav = other["top_nav_categories"]
    cats = {e["category"]: e["n"] for e in nav}
    # 2 political markets map to nav={politics, elections} → each fires twice.
    assert cats.get("politics") == 2
    assert cats.get("elections") == 2
    assert cats.get("sports") == 1


def test_other_bucket_caveat_fires_above_threshold():
    """If OTHER share > 25% of closed, breakdown carries a caveat string."""
    closed = []
    closed += [_pos(outcome="Yes", realizedPnl=10.0, ts=1000 + i) for i in range(3)]
    closed += [_pos(outcome="Trump", realizedPnl=20.0, cid=f"cid_{i}",
                    ts=2000 + i) for i in range(7)]
    osb = _compute_outcome_side_breakdown(closed)
    assert osb["caveat"] is not None
    assert "70.0%" in osb["caveat"]
    assert "OTHER" in osb["caveat"]


def test_other_bucket_caveat_does_not_fire_when_other_is_minor():
    closed = []
    closed += [_pos(outcome="Yes", realizedPnl=10.0, ts=1000 + i) for i in range(20)]
    closed += [_pos(outcome="Trump", realizedPnl=20.0, cid=f"cid_{i}",
                    ts=2000 + i) for i in range(2)]
    osb = _compute_outcome_side_breakdown(closed)
    # OTHER share ~9% < 25% → no caveat.
    assert "caveat" not in osb


def test_other_bucket_detail_renders_in_report():
    closed = []
    closed += [_pos(outcome="Yes", realizedPnl=10.0, ts=1000 + i) for i in range(3)]
    closed += [
        {**_pos(outcome="Trump", realizedPnl=300.0, cid=f"cid_{i}",
                ts=2000 + i),
         "title": f"Will Trump win state {i}?",
         "totalBought": 1000.0 + 100 * i}
        for i in range(7)
    ]
    data = _wallet_data(closed_positions=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    md = render_report(report)
    # OTHER detail sub-section
    assert "Definition: closed positions whose outcome label is not in" in md
    assert "Top outcome labels" in md
    assert "'Trump'" in md
    assert "Top markets by realized P&L" in md
    assert "Will Trump win state" in md
    # CAVEAT line because OTHER is 70% of closed
    assert "CAVEAT:" in md
    assert "OTHER bucket is" in md


def test_outcome_side_breakdown_handles_pre_resolution_exit():
    """Pre-resolution profitable exit: outcome=Yes, curPrice=0.55 (not at
    resolution), realizedPnl > 0. Must count as a win — `_is_win` would
    have called this a loss because curPrice != 1.0."""
    closed = [_pos(outcome="Yes", curPrice=0.55, avgPrice=0.40,
                   realizedPnl=15.0, ts=1000)]
    osb = _compute_outcome_side_breakdown(closed)
    assert osb["yes"]["win_rate"] == 1.0
    assert osb["yes"]["total_pnl"] == pytest.approx(15.0)


def test_outcome_side_breakdown_omits_other_bucket_when_no_other_outcomes():
    closed = [_pos(outcome="Yes", realizedPnl=10.0, ts=1000 + i) for i in range(5)]
    closed += [_pos(outcome="No", realizedPnl=-10.0, ts=2000 + i) for i in range(5)]
    osb = _compute_outcome_side_breakdown(closed)
    assert "other" not in osb
    assert osb["yes"]["n"] == 5
    assert osb["no"]["n"] == 5


def test_outcome_side_breakdown_returns_none_for_empty_closed():
    assert _compute_outcome_side_breakdown([]) is None


def test_outcome_side_breakdown_renders_when_present():
    closed = []
    closed += [_pos(outcome="Yes", curPrice=1.0, avgPrice=0.50, realizedPnl=50.0,
                    ts=1000 + i) for i in range(20)]
    closed += [_pos(outcome="No", curPrice=0.0, avgPrice=0.50, realizedPnl=-50.0,
                    ts=2000 + i) for i in range(20)]
    data = _wallet_data(closed_positions=closed)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert report.outcome_side_breakdown is not None
    md = render_report(report)
    assert "## Edge surface" in md
    assert "### Outcome-side breakdown" in md
    assert "YES: n=20" in md
    assert "NO: n=20" in md


# ---------------------------------------------------------------------------
# Extractor features summary
# ---------------------------------------------------------------------------


def test_extractor_features_summary_renders_when_populated():
    data = _wallet_data(
        extractor_features_summary={
            "usdc_size_cv": 0.45,
            "top_condition_fill_share": 0.31,
            "redeem_ratio": 0.12,
            "domain_concentration": 0.55,
            "pnl_cv_abs": 1.8,
        },
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.extractor_features_summary is not None
    assert report.extractor_features_summary["usdc_size_cv"] == pytest.approx(0.45)
    md = render_report(report)
    assert "### Wallet shape (extractor features)" in md
    assert "usdc_size_cv: 0.4500" in md
    assert "top_condition_fill_share: 0.3100" in md
    assert "redeem_ratio: 0.1200" in md
    assert "domain_concentration: 0.5500" in md
    assert "pnl_cv_abs: 1.8000" in md


def test_extractor_features_summary_omitted_when_absent():
    data = _wallet_data()  # no extractor_features_summary
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.extractor_features_summary is None
    md = render_report(report)
    assert "### Wallet shape" not in md


# ---------------------------------------------------------------------------
# Edge surface composition
# ---------------------------------------------------------------------------


def test_edge_surface_section_renders_when_outcome_breakdown_populated():
    """Even with no extractor features and no grain financials, an outcome
    breakdown alone yields a populated Edge surface section."""
    data = _wallet_data()
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    md = render_report(report)
    assert "## Edge surface" in md
    assert "### Outcome-side breakdown" in md


def test_edge_surface_section_omitted_when_all_inputs_empty():
    """Stage-1 rejected wallet (no closed positions) → no edge surface."""
    data = _wallet_data(closed_positions=[])
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200)
    assert report.outcome_side_breakdown is None
    assert report.extractor_features_summary is None
    md = render_report(report)
    assert "## Edge surface" not in md


# ---------------------------------------------------------------------------
# Per-grain financials (held branch)
# ---------------------------------------------------------------------------


def _grain_friendly_held() -> tuple[list[dict], dict[str, list[str]], dict[str, str]]:
    """Build a held set whose grains will produce non-empty financials.

    Outcomes interleaved so CUSUM doesn't detect a regime change and trim
    positions from the post-regime subset (block of all-wins followed by
    all-losses would). Result: 30 positions, 18 wins / 12 losses.
    """
    closed = []
    # Interleave wins and losses 3-2 so cumulative excess stays bounded.
    win_count = 0
    loss_count = 0
    ts = 1000
    while win_count < 18 or loss_count < 12:
        for _ in range(3):
            if win_count >= 18:
                break
            closed.append(_pos(cid=f"cid_p_w_{win_count}", outcome="Yes",
                               curPrice=1.0, avgPrice=0.40,
                               realizedPnl=60.0, ts=ts))
            ts += 1
            win_count += 1
        for _ in range(2):
            if loss_count >= 12:
                break
            closed.append(_pos(cid=f"cid_p_l_{loss_count}", outcome="Yes",
                               curPrice=0.0, avgPrice=0.40,
                               realizedPnl=-40.0, ts=ts))
            ts += 1
            loss_count += 1
    leafs_by_cid = {p["conditionId"]: ["politics"] for p in closed}
    nav_lookup = {"politics": "politics"}
    return closed, leafs_by_cid, nav_lookup


def test_per_grain_financials_attached_to_held_branch():
    closed, leafs_by_cid, nav_lookup = _grain_friendly_held()
    data = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )

    def peer_fn(grain, tag, wallet):
        return (0.40, 80)

    report = analyze_wallet(
        "0xabc",
        pre_fetched_data=data,
        n_iter=200,
        seed=1,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
        peer_baseline_fn=peer_fn,
    )
    assert report.held_analyst_branch is not None
    grains = report.held_analyst_branch.tag_grains
    assert grains, "expected at least one tag grain"
    g = grains[0]
    # Cardinality from CUSUM-filtered held positions; we don't bind to an
    # exact pre-CUSUM count because regime detection is a separate concern.
    assert g.n_at_grain >= 20
    # Grain financials must mirror the H5 metric_value computed off the same
    # post-CUSUM held subset — they consume the same indices into the same
    # held_positions list.
    assert g.grain_win_rate == pytest.approx(g.metric_value)
    # Constants of the synthetic data: $60 wins and $-40 losses, only.
    assert g.grain_avg_win == pytest.approx(60.0)
    assert g.grain_avg_loss == pytest.approx(-40.0)
    assert g.grain_max_realized_loss == pytest.approx(-40.0)
    # PnL = win_count * 60 + loss_count * (-40); reproduce from grain_win_rate.
    expected_wins = round(g.grain_win_rate * g.n_at_grain)
    expected_losses = g.n_at_grain - expected_wins
    expected_pnl = expected_wins * 60.0 - expected_losses * 40.0
    assert g.grain_total_pnl == pytest.approx(expected_pnl)
    md = render_report(report)
    assert "### Per-grain financials (held)" in md
    assert "avg_win=$60.00" in md
    assert "max_loss=$-40.00" in md


# ---------------------------------------------------------------------------
# Verdict invariance — Edge Surface v1 must NEVER alter verdict signature
# ---------------------------------------------------------------------------


def _verdict_signature(report: ProtocolReport) -> tuple:
    return (
        report.overall_verdict,
        report.gate_result,
        report.primary_edge_type,
        report.copyability_note,
    )


def test_extractor_features_summary_does_not_alter_verdict():
    closed = [_pos(realizedPnl=20.0, ts=1000 + i) for i in range(40)]
    base = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
    )
    enriched = WalletData(
        wallet="0xabc",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        extractor_features_summary={
            "usdc_size_cv": 0.45,
            "top_condition_fill_share": 0.31,
            "redeem_ratio": 0.12,
            "domain_concentration": 0.55,
            "pnl_cv_abs": 1.8,
        },
        execution_profile=None,
        fetched_at=_now(),
    )
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)
    r_enr = analyze_wallet("0xabc", pre_fetched_data=enriched, n_iter=200, seed=1)
    assert _verdict_signature(r_base) == _verdict_signature(r_enr)


def test_outcome_side_breakdown_does_not_alter_verdict():
    """Computing outcome-side breakdown is a side-effect-free read; matched
    runs (same closed set) must produce identical verdicts."""
    closed = [
        _pos(outcome="Yes" if i % 2 == 0 else "No",
             realizedPnl=10.0 if i % 3 else -10.0,
             ts=1000 + i)
        for i in range(40)
    ]
    data = _wallet_data(closed_positions=closed)
    r_a = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    r_b = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert _verdict_signature(r_a) == _verdict_signature(r_b)
    # And the breakdown is populated.
    assert r_a.outcome_side_breakdown is not None


def test_per_grain_financials_do_not_alter_grain_verdict():
    """Attaching descriptive financials must not perturb the H5 statistical
    verdict on the same grain."""
    closed, leafs_by_cid, nav_lookup = _grain_friendly_held()
    data = WalletData(
        wallet="0xabc", closed_positions=closed, open_positions=[],
        activity_rows=[], activity_cap_hit=False,
        archetype_layer1=None, archetype_layer2=None,
        execution_profile=None, fetched_at=_now(),
    )

    def peer_fn(grain, tag, wallet):
        return (0.40, 80)

    report = analyze_wallet(
        "0xabc", pre_fetched_data=data, n_iter=200, seed=1,
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        peer_baseline_fn=peer_fn,
    )
    grains = report.held_analyst_branch.tag_grains
    g = grains[0]
    # H5 hypothesis-test fields independent of financial fields. metric_value
    # IS the grain WR by construction in `_build_one_grain`; assert the two
    # render-paths agree.
    assert g.metric_value == pytest.approx(g.grain_win_rate)
    assert g.pop_baseline == pytest.approx(0.40)
    # Grain financials populated AND statistical verdict still set:
    assert g.grain_total_pnl is not None
    assert g.verdict in ("supported", "inconclusive", "refuted", "descriptive_only")


# ---------------------------------------------------------------------------
# Replay round-trip
# ---------------------------------------------------------------------------


def test_extractor_features_summary_round_trips_via_replay():
    from sniper.analysis.wallet_protocol.replay import (
        deserialize_wallet_data, serialize_wallet_data,
    )
    data = WalletData(
        wallet="0xabc",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        extractor_features_summary={
            "usdc_size_cv": 0.45,
            "top_condition_fill_share": 0.31,
            "redeem_ratio": 0.12,
            "domain_concentration": 0.55,
            "pnl_cv_abs": 1.8,
        },
        execution_profile=None,
        fetched_at=1700000000,
        replay_surface_meta={
            k: {"status": "captured", "captured_at": 1700000000,
                "source": "test", "producer": "test"}
            for k in (
                "archetype_layer1", "execution_profile", "price_history_lookups",
                "leafs_by_cid", "nav_lookup", "peer_baselines",
            )
        },
    )
    payload = serialize_wallet_data(data)
    assert payload["extractor_features_summary"]["usdc_size_cv"] == pytest.approx(0.45)
    restored = deserialize_wallet_data(payload)
    assert restored.extractor_features_summary == data.extractor_features_summary


def test_extractor_features_summary_absent_loads_as_none_for_legacy_payloads():
    """v2 payloads recorded before extractor_features_summary existed must
    still load cleanly with the field=None."""
    from sniper.analysis.wallet_protocol.replay import deserialize_wallet_data
    payload = {
        "schema_version": 2,
        "wallet": "0xabc",
        "fetched_at": 1700000000,
        "closed_positions": [], "open_positions": [], "activity_rows": [],
        "activity_cap_hit": False,
        "archetype_layer1": None,
        "archetype_layer2": None,
        # extractor_features_summary intentionally omitted
        "execution_profile": None,
        "capital_ledger": None,
        "portfolio_value_usdc": None,
        "fetch_quality_flags": [],
        "subgraph_fallback_used": False,
        "price_history_lookups": None,
        "leafs_by_cid": None,
        "nav_lookup": None,
        "peer_baselines": None,
        "replay_surface_meta": {},
    }
    restored = deserialize_wallet_data(payload)
    assert restored.extractor_features_summary is None

from dataclasses import asdict

from sniper.analysis.winning_wallet_thesis.assembler import assemble_profile
from sniper.analysis.winning_wallet_thesis.capital_risk import build_capital_risk_profile
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _ep(cid, event, tag, cost, pnl):
    return PositionEpisode("0xw", cid, event, 0, 0, buy_shares=100, buy_cost=cost,
                           sell_shares=100, sell_proceeds=cost + pnl,
                           resolved=True, won=1, tags=[tag], subcat=tag,
                           category="sports", first_buy_ts=1_700_000_000)


def test_profile_surfaces_loss_center_overallocation():
    sports = _ep("s", "sports_event", "UFC", 100, 20)
    iran = _ep("i", "iran_event", "Iran", 400, -100)
    cluster = EdgeCluster("iran", "Iran", {"tag": "Iran"}, "tag", 1, 1, -100, -10,
                          [(iran.condition_id, iran.outcome_index, iran.episode_index)],
                          is_loss_center=True)
    profile = build_capital_risk_profile("0xw", PositionBook("0xw", [sports, iran]), [cluster])
    row = profile.cluster_risk["iran"]
    assert row.budget_share_pct == 80.0
    assert "capital_overallocated_to_loss_center" in profile.warnings


def test_profile_contains_required_capital_axes():
    fav = _ep("f", "fav_event", "UFC", 80, 20)
    longshot = PositionEpisode("0xw", "l", "long_event", 0, 0, buy_shares=100, buy_cost=5,
                               sell_shares=100, sell_proceeds=30, resolved=True, won=1,
                               tags=["MLB"], subcat="MLB", category="sports",
                               first_buy_ts=1_700_010_800)
    profile = build_capital_risk_profile(
        "0xw", PositionBook("0xw", [fav, longshot]), []
    )
    assert {"category", "tag", "subcat", "template", "price", "mechanism", "hour", "tag_price"} <= set(profile.slices)
    assert {row.slice_key for row in profile.slices["tag"]} == {"UFC", "MLB"}
    assert {row.slice_key for row in profile.slices["price"]} == {"favorite", "deep-longshot"}
    assert any(row.slice_key == "UFC | favorite" for row in profile.slices["tag_price"])


def test_total_is_reaccounted_and_reconciliation_near_zero():
    from sniper.analysis.winning_wallet_thesis.types import LedgerRow, WalletEventLedger, EnrichedLedger
    from sniper.analysis.winning_wallet_thesis import position_builder as PB
    rows = [
        LedgerRow("split", "0xw", "c", 1, size=100, usdc=100, event_slug="e"),
        LedgerRow("merge", "0xw", "c", 2, size=90, usdc=90, event_slug="e"),
    ]
    led = WalletEventLedger("0xw", rows=rows)
    book = PB.build_position_book(EnrichedLedger(
        ledger=led,
        resolutions={"c": {"resolved": True, "winner": 0, "push": False}},
        tags={"e": ["Sports"]}, templates={"e": "x"}))
    profile = build_capital_risk_profile("0xw", book, [], ledger=led)
    # the attributed split/merge cash reconciles to the raw net inventory cash flow
    assert abs(profile.inventory_reconciliation) < 1.0
    assert not hasattr(profile, "inventory_net_cash")        # band-aid field removed
    # split-then-merge on one condition nets ~0 across the YES/NO pair
    assert abs(profile.total_realized_pnl_with_inventory) < 1.0


def test_reconciliation_works_without_ledger_from_baskets():
    from sniper.analysis.winning_wallet_thesis.types import LedgerRow, WalletEventLedger, EnrichedLedger
    from sniper.analysis.winning_wallet_thesis import position_builder as PB
    rows = [LedgerRow("split", "0xw", "c", 1, size=100, usdc=100, event_slug="e"),
            LedgerRow("merge", "0xw", "c", 2, size=90, usdc=90, event_slug="e")]
    book = PB.build_position_book(EnrichedLedger(
        ledger=WalletEventLedger("0xw", rows=rows),
        resolutions={"c": {"resolved": True, "winner": 0, "push": False}},
        tags={"e": ["Sports"]}, templates={"e": "x"}))
    profile = build_capital_risk_profile("0xw", book, [])     # no ledger -> raw_inv from baskets
    assert abs(profile.inventory_reconciliation) < 1.0


def test_profile_computes_peak_deployed_and_drawdown():
    profile = build_capital_risk_profile("0xw", PositionBook("0xw", [_ep("s", "event", "UFC", 100, 20)]), [])
    assert profile.peak_deployed is not None and profile.peak_deployed > 0
    assert profile.max_drawdown is not None
    assert "peak_deployed_unknown" not in profile.warnings


def test_assembler_attaches_capital_risk_profile():
    ep = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=100, buy_cost=50,
                         sell_shares=100, sell_proceeds=60, resolved=True, won=1)
    cluster = EdgeCluster("c", "edge", {}, "tag", 1, 1, 10, 5,
                          [(ep.condition_id, ep.outcome_index, ep.episode_index)])
    profile = assemble_profile("0xw", PositionBook("0xw", [ep]), {}, None, [cluster])
    blob = asdict(profile)
    assert blob["capital_risk"]["total_buy_cost"] == 50

"""Acceptance: the modular pipeline reproduces the prototype's SwissMiss facts on the
cached activity (spec §10). Skips in CI (no live API / no cache)."""
import os

import pytest

from sniper.analysis.winning_wallet_thesis import ledger as L, enrichment as E
from sniper.analysis.winning_wallet_thesis import position_builder as PB, slicer as S
from sniper.analysis.winning_wallet_thesis import clusters as C, assembler as A

SWISS = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"


def _cache_present() -> bool:
    base = os.path.join(L.CACHE_DIR, SWISS.lower())
    return all(os.path.exists(f"{base}_{k}.json") for k in ("activity", "resolutions", "tags"))


@pytest.mark.skipif(not _cache_present(), reason="SwissMiss cache absent (no live API in CI)")
def test_swissmiss_multi_cluster_loss_center_and_pnl_band():
    enriched = E.enrich_ledger(L.build_ledger(SWISS), refresh=False)
    book = PB.build_position_book(enriched)
    tables = S.slice_edges(book)
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    reward = S.reward_attribution(enriched.ledger.raw_activity)["total"]
    rd = book.resolved_directional()
    pnl = sum(e.realized_pnl or 0.0 for e in rd)
    share = S.reward_share_pct(trading_pnl=pnl, reward_total=reward)
    profile = A.assemble_profile(SWISS, book, tables, None, clusters, reward_share=share)

    edge_labels = {c.label for c in profile.edge_clusters}
    loss_labels = {c.label for c in profile.loss_centers}

    # durable platform-tag sports edge clusters present
    assert {"UFC", "MLB", "CFB"}.issubset(edge_labels), edge_labels
    # Iran / Middle East loss center present
    assert {"Iran", "Middle East", "Geopolitics"}.issubset(loss_labels), loss_labels
    # full-book realized PnL in the historically-verified band (+$300k..+$420k)
    assert 300_000 < pnl < 420_000, pnl
    # prediction-driven, not reward-farming
    assert 5.0 < share < 10.0, share
    # the cluster engine emits many clusters (the multi-thesis premise)
    assert len(profile.edge_clusters) >= 10


@pytest.mark.skipif(not _cache_present(), reason="SwissMiss cache absent (no live API in CI)")
def test_swissmiss_split_merge_reaccounting_reconciles_to_realized_total():
    """Track 1 re-accounting parity. The rigorous per-leg attribution reproduces the realized
    total with split/merge cash fully attributed (reconciliation ~ 0).

    Numbers RECALIBRATED 2026-06-16 after the activity fill-dedup fix (`00d77df`): the old
    dedup-by-tx dropped same-tx multi-fill BUY rows, understating SwissMiss's cost basis and
    OVERSTATING her realized PnL. With complete fills cost basis is higher, so realized drops:
    settled ~$359.5k -> ~$322.2k; full (with push/sold-out fold) ~$335.3k -> ~$225.5k. The
    PARITY (reconciliation ~ 0; settled == directional) is unchanged — only the levels moved,
    by design. (SwissMiss was not reconciled vs Polymarket; the dedup fix is validated on
    0x20ad75/0x95074f/0x5bffcf and the direction here — more cost, less PnL — is correct.)"""
    from sniper.analysis.winning_wallet_thesis.capital_risk import build_capital_risk_profile
    enriched = E.enrich_ledger(L.build_ledger(SWISS), refresh=False)
    book = PB.build_position_book(enriched)

    # Full realized total (push losses + cash banked from sold-out positions) -> ~$225.5k post-dedup.
    full = sum(e.realized_pnl or 0.0 for e in book.resolved_with_pnl())
    assert 215_000 < full < 235_000, full

    # Split/merge parity on the SETTLED win/loss population -> ~$322.2k post-dedup.
    settled = sum(e.realized_pnl for e in book.episodes
                  if e.resolved and e.won is not None and not e.is_push
                  and (e.buy_shares > 0 or e.split_shares > 0 or e.merge_shares > 0))
    assert 315_000 < settled < 330_000, settled

    cr = build_capital_risk_profile(SWISS, book, [], ledger=enriched.ledger)
    assert 215_000 < cr.total_realized_pnl_with_inventory < 235_000
    assert abs(cr.inventory_reconciliation) < 1.0               # all split/merge cash attributed

    # the $34M complete-set book on 0xe2bdf6 is OPEN -> its ~$275k residual is unrealized, excluded
    e2 = [e for e in book.episodes if e.condition_id.startswith("0xe2bdf6")]
    assert e2 and all(not e.resolved for e in e2)
    # selection edge population stays purely buy-driven (no structural split/merge leak)
    assert all(e.buy_shares > 0 for e in book.resolved_directional())


@pytest.mark.skipif(not _cache_present(), reason="SwissMiss cache absent (no live API in CI)")
def test_swissmiss_shuffle_baseline_is_descriptive_not_inflated():
    enriched = E.enrich_ledger(L.build_ledger(SWISS), refresh=False)
    book = PB.build_position_book(enriched)
    base = S.shuffle_baseline(book, n_shuffles=20, seed=1)
    # her green-slice count is only modestly above the within-wallet shuffle null
    assert base.observed_green > 0 and base.expected_green > 0
    assert base.obs_over_expected < 3.0          # honest: not a dramatic over-null signal

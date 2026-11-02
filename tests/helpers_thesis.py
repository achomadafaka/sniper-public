"""Tiny builders for analyzer tests — construct PositionBook / PositionEpisode and the
synthesis input views without the full ledger pipeline. Test infrastructure only."""
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.types import PositionBook, PositionEpisode


def make_episode(*, area: str = "x", entry: float = 0.5, cost: float = 1000.0,
                 won: int | None = 1, slug: str = "e", cid: str | None = None, oidx: int = 0,
                 first_buy_ts: int = 0, resolution_ts: int = 0, resolved: bool = True,
                 sell_shares: float = 0.0, idx: int = 0) -> PositionEpisode:
    shares = cost / entry if entry > 0 else 0.0
    return PositionEpisode(
        wallet="0xtest", condition_id=(cid or slug), event_slug=slug, outcome_index=oidx,
        episode_index=idx, buy_shares=shares, buy_cost=cost, sell_shares=sell_shares,
        resolved=resolved, won=(None if won is None else int(won)), is_push=False,
        first_buy_ts=first_buy_ts, resolution_ts=resolution_ts, category=area, tags=[area],
    )


def make_book(rows: list[dict]) -> PositionBook:
    eps = [make_episode(idx=i, **r) for i, r in enumerate(rows)]
    return PositionBook(wallet="0xtest", episodes=eps)


def make_profile(*, p_value: float = 1.0, obs_over_expected: float = 1.0,
                 edge_labels: tuple = (), loss_labels: tuple = ()):
    """Minimal ThesisProfile for orchestrator/mapping tests."""
    from sniper.analysis.winning_wallet_thesis.types import (
        ThesisProfile, ShuffleBaseline, EdgeCluster)
    sb = ShuffleBaseline(n_shuffles=200, expected_green=1.0, observed_green=1,
                         obs_over_expected=obs_over_expected, top_slice_rank_under_null=0.5,
                         p_value=p_value, null_p95=1.0)

    def _cluster(label, loss):
        return EdgeCluster(cluster_id=label, label=label, scope={}, axis="tag", n_positions=20,
                           n_events=10, pnl=(1.0 if not loss else -1.0),
                           edge_pp=5.0, is_loss_center=loss)

    edges = [_cluster(lbl, False) for lbl in edge_labels]
    losses = [_cluster(lbl, True) for lbl in loss_labels]
    return ThesisProfile(wallet="0xtest", run_id="run1", headline="h", edge_clusters=edges,
                         loss_centers=losses, shuffle_baseline=sb)


def fresh_view(**kw):
    """A FreshAssessment-like stub for synthesis tests. Defaults to a non-assessable
    fresh read; pass fields to exercise the fresh branch."""
    from sniper.analysis.winning_wallet_thesis.fresh_anomaly import FreshAssessment
    defaults = dict(assessable=False, history_thin=True, channels_strong=0, high_surprise=False,
                    alt_explanation=None, e_value=1.0, ex_post_level="none")
    defaults.update(kw)
    if defaults.get("high_surprise") is None:
        defaults["high_surprise"] = defaults.get("ex_post_level") == "strong"
    return FreshAssessment(**defaults)

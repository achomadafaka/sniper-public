from __future__ import annotations

from dataclasses import dataclass

from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode


def _key(ep: PositionEpisode) -> tuple:
    return (ep.condition_id, ep.outcome_index, ep.episode_index)


@dataclass(frozen=True)
class ClusterContext:
    cluster: EdgeCluster
    book: PositionBook

    @property
    def episodes(self) -> list[PositionEpisode]:
        wanted = set(self.cluster.member_position_keys)
        if not wanted:
            return []
        return [ep for ep in self.book.episodes if _key(ep) in wanted]

    @property
    def resolved_episodes(self) -> list[PositionEpisode]:
        return [ep for ep in self.episodes if ep.resolved and ep.won is not None and not ep.is_push]

    @property
    def event_count(self) -> int:
        return len({ep.event_slug for ep in self.episodes if ep.event_slug})

    @property
    def turnover(self) -> float:
        return sum(ep.buy_cost for ep in self.episodes)

    @property
    def pnl(self) -> float:
        return sum(ep.realized_pnl or 0.0 for ep in self.resolved_episodes)

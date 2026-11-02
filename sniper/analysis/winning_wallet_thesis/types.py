"""Data objects for the winning-wallet thesis extractor (spec rev 2 §8). No logic
beyond pure derived fields; ingestion/edge decisions live in the other modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RESOLUTION_WIN = 0.97
RESOLUTION_LOSS = 0.03


@dataclass
class LedgerRow:
    kind: str                       # buy|sell|redeem|split|merge|reward
    wallet: str
    condition_id: str               # may be empty for wallet-level reward/yield rows
    ts: int
    seq: int = 0                    # original feed order — same-ts rows stay deterministic
    outcome_index: int | None = None
    outcome: str = ""
    price: float = 0.0
    size: float = 0.0
    usdc: float = 0.0
    tx: str = ""
    event_slug: str = ""
    title: str = ""
    episode_index: int | None = None    # which directional episode this buy/sell landed in (set by position_builder)


@dataclass
class WalletEventLedger:
    wallet: str
    rows: list[LedgerRow] = field(default_factory=list)
    raw_activity: list[dict] = field(default_factory=list)   # kept for reward attribution


@dataclass
class EnrichedLedger:
    ledger: WalletEventLedger
    resolutions: dict[str, dict] = field(default_factory=dict)   # condition_id -> {resolved,winner,push}
    tags: dict[str, list[str]] = field(default_factory=dict)     # event_slug -> [tag]
    templates: dict[str, str] = field(default_factory=dict)      # event_slug -> template
    conversions: list = field(default_factory=list)              # [(ts:int, ConversionEffect)] negRisk convertPositions


@dataclass
class PositionEpisode:
    wallet: str
    condition_id: str
    event_slug: str
    outcome_index: int
    episode_index: int
    outcome: str = ""
    title: str = ""
    buy_shares: float = 0.0
    buy_cost: float = 0.0
    sell_shares: float = 0.0
    sell_proceeds: float = 0.0
    split_shares: float = 0.0
    split_cost: float = 0.0          # cost basis allocated to this leg's split-minted shares
    merge_shares: float = 0.0
    merge_cash: float = 0.0          # cash proceeds allocated to this leg on MERGE
    convert_in_shares: float = 0.0   # YES shares minted to this leg by a negRisk CONVERSION
    convert_in_cost: float = 0.0     # cost basis transferred in (burned-NO cost - collateral, allocated)
    convert_out_shares: float = 0.0  # NO shares burned out of this leg by a CONVERSION
    convert_out_cost: float = 0.0    # cost basis released when those NO shares were burned
    convert_cash: float = 0.0        # collateral cash booked here on a zero-YES conversion (NO sold for collateral)
    redeemed: bool = False
    first_buy_ts: int | None = None
    last_ts: int = 0
    resolved: bool = False
    won: int | None = None
    is_push: bool = False
    resolution_ts: int | None = None    # actual market close time (Gamma closedTime)
    # semantic (filled by enrichment mapping)
    category: str = ""
    subcat: str = ""
    tags: list[str] = field(default_factory=list)
    template: str = ""

    @property
    def entry_vwap(self) -> float:
        return self.buy_cost / self.buy_shares if self.buy_shares > 0 else 0.0

    @property
    def is_directional(self) -> bool:
        return self.buy_shares > 0

    @property
    def held_shares(self) -> float:
        """Shares still held in this leg, INCLUDING negRisk conversion flow (convert_in mints YES,
        convert_out burns NO). Single source for the 'still open?' check used by the store, the CLI
        live-mark fetch, and the live-positions known/new label — those previously dropped the
        convert terms and mislabeled conversion-held positions as closed."""
        return max(self.buy_shares + self.split_shares + self.convert_in_shares
                   - self.sell_shares - self.merge_shares - self.convert_out_shares, 0.0)

    @property
    def exit_mechanism(self) -> str:
        if not self.resolved and not self.redeemed:
            return "open"
        if self.sell_shares > 0 and self.sell_shares >= 0.5 * max(self.buy_shares, 1e-9):
            return "sold"
        if self.redeemed or self.resolved:
            return "held"
        return "sold" if self.sell_shares > 0 else "open"

    @property
    def realized_pnl(self) -> float | None:
        # Per-leg cash PnL = cash in - cash out. Includes split-minted shares (split_cost) and
        # merge proceeds (merge_cash). Returns None ONLY when PnL is not yet realized — an
        # unresolved position that still holds shares. Three realized cases:
        #   settled win/loss -> held pays 1.0 (won) or 0.0 (lost)
        #   push             -> held pays 0.5/share (a real gain/loss vs entry, NOT break-even)
        #   sold out early   -> unresolved but no shares left; only the banked sell/merge cash
        # convert_in adds minted-YES shares + transferred cost; convert_out removes burned-NO
        # shares and RELEASES their cost basis (it moved to the minted YES legs of that conversion).
        total_in = self.buy_shares + self.split_shares + self.convert_in_shares
        total_cost = self.buy_cost + self.split_cost + self.convert_in_cost - self.convert_out_cost
        held = max(total_in - self.sell_shares - self.merge_shares - self.convert_out_shares, 0.0)
        if self.resolved and self.won is not None:
            payout = held * (1.0 if self.won else 0.0)
        elif self.resolved and self.is_push:
            payout = held * 0.5
        elif not self.resolved and held <= 1e-9:
            payout = 0.0
        else:
            return None
        # convert_cash = collateral from a zero-YES conversion (the burned NO sold for collateral;
        # its cost basis is KEPT here, not released, so this nets to collateral − cost).
        return self.sell_proceeds + self.merge_cash + self.convert_cash + payout - total_cost


@dataclass
class EventBasket:
    event_slug: str
    condition_ids: set[str] = field(default_factory=set)
    net_cost: float = 0.0
    net_proceeds: float = 0.0
    redeem_payout: float = 0.0
    split_shares: float = 0.0
    merge_shares: float = 0.0
    rewards: float = 0.0
    final_pnl: float | None = None


@dataclass
class PositionBook:
    wallet: str
    episodes: list[PositionEpisode] = field(default_factory=list)
    baskets: dict[str, EventBasket] = field(default_factory=dict)
    reward_income: dict[str, float] = field(default_factory=dict)

    def resolved_directional(self) -> list[PositionEpisode]:
        """Selection population — directional BETS (buy-driven). Used by the edge slicer.
        Structural split/merge legs are deliberately excluded (they are not picks)."""
        return [e for e in self.episodes
                if e.is_directional and e.resolved and e.won is not None and not e.is_push]

    def resolved_with_pnl(self) -> list[PositionEpisode]:
        """Complete realized population for the WALLET TOTAL: every episode that has BOOKED a
        realized PnL — settled wins/losses, pushes (0.5/share refund), structural split/merge
        legs, and positions sold out before resolution. Still-open positions (realized_pnl is
        None) are excluded. Superset of resolved_directional()."""
        return [e for e in self.episodes
                if e.realized_pnl is not None
                and (e.buy_shares > 0 or e.split_shares > 0 or e.merge_shares > 0
                     or e.convert_in_shares > 0 or e.convert_out_shares > 0 or e.convert_cash != 0)]


@dataclass
class EdgeSlice:
    slice_key: str
    axis: str
    n_positions: int = 0
    n_events: int = 0
    wins: int = 0
    entry_sum: float = 0.0
    pnl: float = 0.0
    cost: float = 0.0
    members: list[PositionEpisode] = field(default_factory=list)

    @property
    def win_rate(self) -> float: return self.wins / self.n_positions if self.n_positions else 0.0
    @property
    def mean_entry(self) -> float: return self.entry_sum / self.n_positions if self.n_positions else 0.0
    @property
    def edge_pp(self) -> float: return (self.win_rate - self.mean_entry) * 100.0
    @property
    def roi_pct(self) -> float: return self.pnl / self.cost * 100.0 if self.cost else 0.0


@dataclass
class ShuffleBaseline:
    n_shuffles: int
    expected_green: float
    observed_green: int
    obs_over_expected: float
    top_slice_rank_under_null: float
    p_value: float = 1.0          # P(random produces ≥ her green-slice count) — lower = more real
    null_p95: float = 0.0         # 95th-percentile green-slice count under the null (a CI ceiling)


@dataclass
class EdgeCluster:
    cluster_id: str
    label: str
    scope: dict[str, Any]
    axis: str
    n_positions: int
    n_events: int
    pnl: float
    edge_pp: float
    member_position_keys: list[tuple] = field(default_factory=list)
    is_loss_center: bool = False
    overlap: list[dict] = field(default_factory=list)


@dataclass
class CapitalRiskSlice:
    slice_key: str
    axis: str
    n_positions: int
    n_events: int
    buy_cost: float
    budget_share_pct: float
    realized_pnl: float
    turnover_roi_pct: float
    avg_bet: float
    median_bet: float
    p90_bet: float
    max_bet: float
    bet_cv: float
    top_event_budget_share_pct: float
    peak_deployed: float | None
    roi_on_peak_deployed_pct: float | None
    max_drawdown: float | None
    exit_value_added: float | None
    loser_rescue_value: float | None
    winner_upside_lost: float | None


@dataclass
class ExitValueSummary:
    n_sold: int
    n_sold_losers: int
    n_sold_winners: int
    sell_vs_hold_value: float
    loser_rescue_value: float
    winner_upside_lost: float


@dataclass
class CapitalRiskProfile:
    wallet: str
    total_buy_cost: float
    total_realized_pnl: float
    slices: dict[str, list[CapitalRiskSlice]] = field(default_factory=dict)
    cluster_risk: dict[str, CapitalRiskSlice] = field(default_factory=dict)
    exit_summary: ExitValueSummary | None = None
    warnings: list[str] = field(default_factory=list)
    peak_deployed: float | None = None                  # max capital simultaneously at risk
    roi_on_peak_deployed_pct: float | None = None        # return on capital actually tied up
    max_drawdown: float | None = None                    # realized-equity give-back (not MTM)
    total_realized_pnl_with_inventory: float = 0.0       # full wallet realized PnL (selection + structural split/merge)
    inventory_reconciliation: float = 0.0                # raw net inventory cash - attributed; ~0 guard


@dataclass
class ThesisProfile:
    wallet: str
    run_id: str
    headline: str
    edge_clusters: list[EdgeCluster] = field(default_factory=list)
    loss_centers: list[EdgeCluster] = field(default_factory=list)
    reward_share_pct: float = 0.0
    shuffle_baseline: ShuffleBaseline | None = None
    data_gaps: list[str] = field(default_factory=list)
    method_findings: dict[str, list] = field(default_factory=dict)   # cluster_id -> [MethodFinding]
    validation_plans: list = field(default_factory=list)             # [ValidationPlan]
    capital_risk: "CapitalRiskProfile | None" = None                 # per-slice budget/risk decomposition

"""Backtest harness — replay a wallet_rule_v1 against historical positions.

Path A step 2: given a ``wallet_rule_v1`` (from
``wallet_parameter_extractor``) and the wallet's own historical position
set, replay the rule with explicit in-sample / out-of-sample split and a
slippage model. Computes hypothetical PnL, compares to the wallet's
actual PnL on the same positions, and reports OOS metrics that an
operator can use to decide whether to forward-test on paper.

Purely offline. No live API calls. No DB writes.

Doctrine:
- This is NOT a deploy decision. Even a strong OOS result is one of
  many gates before paper validation; live execution is a separate spec.
- "Match" only tells us the rule's market_filter agreed with the wallet's
  entry. The harness assumes hold-to-resolution per the rule's
  exit_rule.mode == "held" (other modes are not supported in v1).
- Slippage is a flat percentage applied to entry price. Real CLOB
  slippage is asymmetric (worse when liquidity is thin); the operator
  should treat the slippage parameter as a stress dial, not a fact.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from sniper.analysis.wallet_dossier.derivers import (
    derive_favorite_status_and_rank,
    derive_market_type,
    derive_outcome_role,
)

logger = logging.getLogger(__name__)

HARNESS_VERSION = "backtest_v1"


@dataclass(slots=True)
class BacktestResult:
    """Structured output of one backtest run."""

    harness_version: str = HARNESS_VERSION
    rule_wallet: str = ""
    rule_grain: dict[str, Any] = field(default_factory=dict)
    oos_split_ratio: float = 0.25
    slippage_pct: float = 0.0
    rule_position_size_usdc: float = 0.0

    n_positions_total: int = 0
    n_in_sample: int = 0
    n_out_of_sample: int = 0

    in_sample_match_count: int = 0
    oos_match_count: int = 0
    oos_skipped_unresolved: int = 0

    oos_simulated_pnl_usdc: float = 0.0
    oos_simulated_wins: int = 0
    oos_simulated_losses: int = 0
    oos_simulated_win_rate: float | None = None

    oos_actual_pnl_usdc_on_same_positions: float = 0.0
    pnl_vs_wallet_delta_usdc: float = 0.0

    caveats: list[str] = field(default_factory=list)
    per_position_results: list[dict[str, Any]] = field(default_factory=list)


def backtest_rule_on_wallet(
    rule: Mapping[str, Any],
    wallet_data: Mapping[str, Any],
    *,
    oos_split_ratio: float = 0.25,
    slippage_pct: float = 0.005,
    include_per_position: bool = False,
) -> dict[str, Any]:
    """Replay ``rule`` on ``wallet_data``'s historical closed_positions.

    Args:
        rule: A ``wallet_rule_v1`` dict (from wallet_parameter_extractor).
        wallet_data: A WalletData fixture dict — needs ``closed_positions``
            with ``timestamp`` + ``avgPrice`` + ``realizedPnl`` + ``totalBought``
            + ``conditionId`` + ``asset`` (the live Polymarket shape).
            If the fixture has ``leafs_by_cid`` (a conditionId → tag-list
            map produced by ``refresh_fixture_tags.py``), category filtering
            uses that lookup; otherwise positions must carry an explicit
            ``market_category`` field.
        oos_split_ratio: Fraction of chronologically-latest positions to
            hold as out-of-sample (default 0.25 = newest 25% withheld).
        slippage_pct: Flat percentage added to entry_price on simulated
            fills (default 0.5%). Stress dial — real slippage is asymmetric.
        include_per_position: When True, populate ``per_position_results``
            with per-trade rows. Off by default for memory.

    Returns:
        A dict view of BacktestResult.
    """
    leafs_by_cid = wallet_data.get("leafs_by_cid") or {}
    result = BacktestResult(
        rule_wallet=str(rule.get("wallet") or ""),
        rule_grain=dict(rule.get("source_grain") or {}),
        oos_split_ratio=float(oos_split_ratio),
        slippage_pct=float(slippage_pct),
    )

    market_filter = rule.get("market_filter") or {}
    sizing = rule.get("sizing") or {}
    exit_rule = rule.get("exit_rule") or {}

    if not market_filter:
        result.caveats.append("rule_has_no_market_filter")
        return asdict(result)
    if exit_rule.get("mode") != "held":
        result.caveats.append(f"unsupported_exit_mode:{exit_rule.get('mode')}")
        return asdict(result)

    rule_size = float(sizing.get("median_usdc") or 0.0)
    if rule_size <= 0:
        rule_size = 10.0  # fallback so the harness still runs
        result.caveats.append("rule_size_missing_using_10usdc_default")
    result.rule_position_size_usdc = rule_size

    closed = list(wallet_data.get("closed_positions") or [])
    if not closed:
        result.caveats.append("no_closed_positions")
        return asdict(result)

    # Chronological order (oldest first); positions without timestamp drop to end
    closed_sorted = sorted(
        closed,
        key=lambda p: int(p.get("timestamp") or 0),
    )
    result.n_positions_total = len(closed_sorted)

    split_idx = max(0, int(len(closed_sorted) * (1.0 - oos_split_ratio)))
    in_sample = closed_sorted[:split_idx]
    oos = closed_sorted[split_idx:]
    result.n_in_sample = len(in_sample)
    result.n_out_of_sample = len(oos)

    activity_rows = wallet_data.get("activity_rows") or []

    # Codex 2026-05-27 perf: derive_market_type scans every closed_position
    # and activity_row on each call. With N positions and ~3 filter checks
    # per position, runtime was O(3N²). Memoize by conditionId once: per
    # condition, derive_market_type yields the same answer regardless of
    # which row we're filtering. SwissMiss 1011-position smoke: ~500K
    # iterations → ~700 distinct conditions × one call each.
    market_type_cache: dict[str, str] = {}

    def _cached_market_type(condition_id: str) -> str:
        if condition_id not in market_type_cache:
            mt, _src, _n = derive_market_type(
                condition_id,
                closed_positions=closed_sorted,
                activity_rows=activity_rows,
            )
            market_type_cache[condition_id] = str(mt)
        return market_type_cache[condition_id]

    # Count in-sample matches (informational; not used for OOS PnL)
    for pos in in_sample:
        if _position_matches_filter(
            pos, market_filter,
            leafs_by_cid=leafs_by_cid,
            market_type_lookup=_cached_market_type,
        ):
            result.in_sample_match_count += 1

    # OOS replay
    for pos in oos:
        if not _position_matches_filter(
            pos, market_filter,
            leafs_by_cid=leafs_by_cid,
            market_type_lookup=_cached_market_type,
        ):
            continue
        result.oos_match_count += 1

        final_price = _terminal_price(pos)
        if final_price is None:
            result.oos_skipped_unresolved += 1
            continue

        avg_price = float(pos.get("avgPrice") or 0.0)
        # Polymarket /closed-positions returns `totalBought` as the gross
        # share count over the position's lifetime, NOT USDC notional.
        # Verified in sniper/analysis/wallet_dossier/builder.py:288.
        actual_size_shares = float(pos.get("totalBought") or 0.0)
        actual_pnl = float(pos.get("realizedPnl") or 0.0)

        # Simulated fill: entry_price + slippage (worse for the buyer)
        sim_entry = min(0.999, avg_price * (1.0 + slippage_pct))
        sim_shares = rule_size / max(sim_entry, 1e-9)
        sim_pnl = (final_price - sim_entry) * sim_shares

        # Win/loss on the simulation
        if sim_pnl > 0:
            result.oos_simulated_wins += 1
        elif sim_pnl < 0:
            result.oos_simulated_losses += 1

        # "Wallet's actual PnL on this position, scaled to our size" — comparable basis
        if actual_size_shares > 0:
            actual_pnl_scaled = actual_pnl * (sim_shares / actual_size_shares)
        else:
            actual_pnl_scaled = 0.0

        result.oos_simulated_pnl_usdc += sim_pnl
        result.oos_actual_pnl_usdc_on_same_positions += actual_pnl_scaled

        if include_per_position:
            result.per_position_results.append({
                "condition_id": str(pos.get("conditionId") or ""),
                "asset_id": str(pos.get("asset") or ""),
                "entry_avg_price": avg_price,
                "sim_entry_price": sim_entry,
                "final_price": final_price,
                "sim_pnl_usdc": sim_pnl,
                "actual_pnl_scaled_usdc": actual_pnl_scaled,
            })

    n_decided = result.oos_simulated_wins + result.oos_simulated_losses
    if n_decided > 0:
        result.oos_simulated_win_rate = result.oos_simulated_wins / n_decided

    result.pnl_vs_wallet_delta_usdc = (
        result.oos_simulated_pnl_usdc - result.oos_actual_pnl_usdc_on_same_positions
    )

    if result.oos_match_count == 0:
        result.caveats.append("no_oos_matches_for_rule")
    if result.oos_skipped_unresolved > 0:
        result.caveats.append(
            f"skipped_unresolved:{result.oos_skipped_unresolved}"
        )

    return asdict(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _position_matches_filter(
    position: Mapping[str, Any],
    market_filter: Mapping[str, Any],
    *,
    leafs_by_cid: Mapping[str, Any] | None = None,
    all_closed_positions: list[Mapping[str, Any]] | None = None,
    all_activity_rows: list[Mapping[str, Any]] | None = None,
    market_type_lookup=None,
) -> bool:
    """Apply a rule's market_filter to one closed_position.

    Category resolution order:
      1. ``position["market_category"]`` if explicitly tagged
      2. ``leafs_by_cid[conditionId]`` — searched for matching category
      3. ``"data_unavailable"`` (which fails any non-empty category filter)

    Market_type + outcome_role: prefer ``market_type_lookup(condition_id)``
    when provided (memoized at the caller). Otherwise derive on-the-fly
    from ``all_closed_positions`` + ``all_activity_rows``. If neither is
    provided, falls back to explicit position fields or ``"unknown"``
    (which fails any non-empty filter for those dimensions).
    """
    def _lookup_market_type(cond_id: str) -> str:
        if market_type_lookup is not None:
            return str(market_type_lookup(cond_id)).lower()
        mt_derived, _src, _n = derive_market_type(
            cond_id,
            closed_positions=list(all_closed_positions or []),
            activity_rows=list(all_activity_rows or []),
        )
        return str(mt_derived).lower()

    # Category
    categories = market_filter.get("categories") or []
    if categories:
        cat = str(position.get("market_category") or "").lower()
        if not cat or cat == "data_unavailable":
            cond = str(position.get("conditionId") or "")
            tags = (leafs_by_cid or {}).get(cond) or []
            tag_set = {str(t).lower() for t in tags}
            wanted = {c.lower() for c in categories}
            if not (wanted & tag_set):
                return False
        elif cat not in [c.lower() for c in categories]:
            return False

    # Entry price range (exclusive upper)
    price_range = market_filter.get("entry_price_range")
    if price_range and len(price_range) == 2:
        avg = float(position.get("avgPrice") or 0.0)
        lo, hi = float(price_range[0]), float(price_range[1])
        if not (lo <= avg < hi):
            return False

    # Market type
    market_types = market_filter.get("market_types") or []
    if market_types:
        mt = str(position.get("market_type") or "").lower()
        if not mt or mt == "unknown":
            cond = str(position.get("conditionId") or "")
            mt = _lookup_market_type(cond)
        if mt not in [m.lower() for m in market_types]:
            return False

    # Outcome role
    outcome_roles = market_filter.get("outcome_roles") or []
    if outcome_roles:
        role = str(position.get("outcome_role") or "").lower()
        if not role or role == "unknown":
            cond = str(position.get("conditionId") or "")
            mt_for_role = _lookup_market_type(cond)
            # derive_outcome_role takes the typed MarketType; cast back from
            # the lowercased cache string.
            role_derived = derive_outcome_role(
                market_type=mt_for_role,  # type: ignore[arg-type]
                outcome_label=position.get("outcome"),
            )
            role = str(role_derived).lower()
        if role not in [r.lower() for r in outcome_roles]:
            return False

    # Favorite status (Codex round 2 — extractor emits this but matcher missed it)
    # A None / "data_unavailable" rule value means "no constraint" — common
    # for pre-v3 grain keys.
    fav_required = market_filter.get("favorite_status")
    if fav_required and str(fav_required).lower() not in ("data_unavailable", "unknown"):
        fav_pos = str(position.get("favorite_status") or "").lower()
        if not fav_pos or fav_pos == "unknown":
            cond = str(position.get("conditionId") or "")
            mt_for_fav = _lookup_market_type(cond)
            fav_derived, _rank, _src = derive_favorite_status_and_rank(
                market_type=mt_for_fav,  # type: ignore[arg-type]
                entry_price=position.get("avgPrice"),
            )
            fav_pos = str(fav_derived).lower()
        if fav_pos != str(fav_required).lower():
            return False

    # Event family (Codex round 2 — same)
    # v1 grain keys carry event_family="data_unavailable" because v1 deferred
    # event-family derivation. Treat that AND None as "no constraint."
    ef_required = market_filter.get("event_family")
    if ef_required and str(ef_required).lower() not in ("data_unavailable", "unknown"):
        ef_pos = str(position.get("event_family") or "").lower()
        if not ef_pos or ef_pos in ("data_unavailable", "unknown"):
            # No way to derive event_family without market metadata; the rule
            # required a specific value so we cannot confirm a match → reject.
            return False
        if ef_pos != str(ef_required).lower():
            return False

    return True


def _terminal_price(position: Mapping[str, Any]) -> float | None:
    """Best-effort terminal price for a closed position.

    Polymarket's closed_positions include curPrice which is typically
    the resolution price (0.0 or 1.0 after settlement). Fall back to
    realizedPnl-implied resolution if curPrice is absent.
    """
    cur = position.get("curPrice")
    if cur is not None:
        try:
            return float(cur)
        except (TypeError, ValueError):
            pass

    # Fallback: infer resolution from realizedPnl sign + avgPrice.
    # Polymarket /closed-positions totalBought is gross share count (verified
    # in sniper/analysis/wallet_dossier/builder.py:288), not USDC notional.
    avg = float(position.get("avgPrice") or 0.0)
    pnl = float(position.get("realizedPnl") or 0.0)
    shares = float(position.get("totalBought") or 0.0)
    if shares <= 0 or avg <= 0:
        return None
    # If pnl per share == (1 - avg), resolution is 1.0; if pnl per share == -avg, resolution is 0.0
    pnl_per_share = pnl / shares
    if abs(pnl_per_share - (1.0 - avg)) < 0.05:
        return 1.0
    if abs(pnl_per_share - (-avg)) < 0.05:
        return 0.0
    return None


__all__ = ["backtest_rule_on_wallet", "BacktestResult", "HARNESS_VERSION"]

"""Sports line dislocation v1 strategy plugin.

Compares bookmaker consensus implied probabilities against Polymarket
binary game-winner prices.  Enters paper trades when Polymarket is
materially underpriced relative to sportsbook fair value.
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .base import (
    StrategyPlugin,
    StrategySignal,
    StrategyEntryDecision,
    StrategyManageState,
    StrategyExitDecision,
    RESOLUTION_WIN,
    RESOLUTION_LOSS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_params(strategy: Dict[str, Any]) -> Dict[str, Any]:
    params = strategy.get("parameters") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            return {}
    return params if isinstance(params, dict) else {}


def _parse_min_books_override(params: Dict[str, Any]) -> Dict[str, int]:
    """Parse min_books_override from strategy params.

    Accepts a dict or JSON string like {"basketball_nba": 3}.
    Returns a plain {sport_key: int} dict.
    """
    raw = params.get("min_books_override", DEFAULTS.get("min_books_override", {}))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    return {k: int(v) for k, v in raw.items()}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def remove_vig_binary(raw_prob_home: float, raw_prob_away: float) -> Tuple[float, float]:
    """Remove vig from a binary h2h market.

    Returns (fair_prob_home, fair_prob_away) summing to 1.0.
    """
    total = raw_prob_home + raw_prob_away
    if total <= 0:
        return (0.5, 0.5)
    return (raw_prob_home / total, raw_prob_away / total)


def compute_consensus(fair_probs: List[float]) -> Dict[str, Any]:
    """Compute median and dispersion from a list of fair probabilities.

    Returns {median, dispersion, books_used}.
    """
    if not fair_probs:
        return {"median": 0.0, "dispersion": 0.0, "books_used": 0}
    med = statistics.median(fair_probs)
    disp = max(fair_probs) - min(fair_probs)
    return {
        "median": round(med, 6),
        "dispersion": round(disp, 6),
        "books_used": len(fair_probs),
    }


def compute_confidence_tier(edge_pts: float, books_used: int) -> Dict[str, Any]:
    """Compute confidence tier from edge strength and book depth.

    Returns {tier, confidence_score, size_multiplier}.
    A 4% edge with 8 books is more trustworthy than 6% with 3 books.
    """
    # Book depth factor: 4 books = 1.0, scales up to 1.5 at 9+ books
    book_factor = min(1.5, max(0.5, (books_used - 2) / 4))

    # Edge factor: 4% = 1.0, 6% = 1.5, 10%+ = 2.0
    edge_factor = min(2.0, max(0.5, edge_pts / 0.04))

    # Combined confidence score (0-3 range)
    confidence_score = round(book_factor * edge_factor, 3)

    if confidence_score >= 2.0:
        tier = "high"
        size_mult = 1.5
    elif confidence_score >= 1.2:
        tier = "medium"
        size_mult = 1.0
    else:
        tier = "low"
        size_mult = 0.5

    return {
        "tier": tier,
        "confidence_score": confidence_score,
        "size_multiplier": size_mult,
        "book_factor": round(book_factor, 3),
        "edge_factor": round(edge_factor, 3),
    }


def _map_teams_to_outcomes(
    home_team: str,
    away_team: str,
    poly_outcomes: List[Dict[str, Any]],
) -> Dict[str, str]:
    """Map sportsbook team names to Polymarket outcome labels.

    Returns {team_lower: outcome_label} for teams that match.
    Polymarket binary winner markets use team names (not Yes/No) as outcomes.
    Falls back to Yes/No if only those are available.
    """
    if not poly_outcomes:
        return {}

    home_lower = home_team.strip().lower()
    away_lower = away_team.strip().lower()
    home_short = home_lower.split()[-1] if home_lower else ""
    away_short = away_lower.split()[-1] if away_lower else ""

    result: Dict[str, str] = {}
    outcome_labels = [o.get("outcome", "") for o in poly_outcomes]

    for label in outcome_labels:
        label_lower = label.lower()
        if home_lower in label_lower or home_short in label_lower:
            result[home_lower] = label
        elif away_lower in label_lower or away_short in label_lower:
            result[away_lower] = label

    # Fallback: if outcomes are just Yes/No, map home=Yes, away=No
    if not result and set(o.lower() for o in outcome_labels) == {"yes", "no"}:
        for label in outcome_labels:
            if label.lower() == "yes":
                result[home_lower] = label
            else:
                result[away_lower] = label

    return result


# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------

DEFAULTS = {
    "bet_size_usdc": 10.0,
    "max_concurrent_positions": 5,
    "max_exposure_per_market_usdc": 50.0,
    "max_entry_price": 0.75,
    "min_edge_pts": 0.04,
    "min_books": 4,
    "min_books_override": {},  # per-sport override e.g. {"basketball_nba": 3}
    "max_consensus_dispersion_pts": 0.04,
    "min_minutes_before_start": 15,
    "hours_ahead": 72,
    "close_gap_pts": 0.02,
    "postponed_grace_hours": 24,
    "sport_keys": "basketball_nba,basketball_ncaab",
}


# ---------------------------------------------------------------------------
# Import live-price helper
# ---------------------------------------------------------------------------

from sniper.markets.polymarket_api import fetch_live_clob_price
from sniper.settings.web import INSTANCE_TAG


class SportsLineDislocationV1Strategy(StrategyPlugin):
    """Buy underpriced Polymarket outcomes vs. bookmaker consensus."""

    plugin_id = "sports_line_dislocation_v1"
    display_name = "Sports Line Dislocation v1"
    description = (
        "Compares bookmaker consensus fair probabilities against Polymarket "
        "binary game-winner prices and paper-trades dislocations."
    )
    strategy_family = "sports_line_dislocation"
    required_feeds = ("polymarket", "odds")
    supported_execution_modes = ("paper",)
    default_loop_seconds = 300

    def validate_parameters(self, parameters: dict) -> List[str]:
        errors = []
        for key in ("min_edge_pts", "min_books", "max_consensus_dispersion_pts"):
            val = parameters.get(key)
            if val is not None:
                try:
                    float(val)
                except (TypeError, ValueError):
                    errors.append(f"{key} must be numeric")
        return errors

    def replay_adapter(self):
        return None  # v1: no replay until historical odds are available

    def discover(
        self,
        conn,
        strategy: Dict[str, Any],
    ) -> List[StrategySignal]:
        from sniper.context.sports_context import (
            recent_odds_events,
            find_matching_polymarket_h2h_markets,
            polymarket_market_outcomes,
            insider_convergence_check,
        )
        from db import ensure_edge_observations_table, ensure_poly_price_snapshots_table
        ensure_edge_observations_table(conn)
        ensure_poly_price_snapshots_table(conn)

        params = _parse_params(strategy)
        min_edge = _to_float(params.get("min_edge_pts", DEFAULTS["min_edge_pts"]))
        min_books = int(_to_float(params.get("min_books", DEFAULTS["min_books"])))
        min_books_override = _parse_min_books_override(params)
        max_disp = _to_float(
            params.get("max_consensus_dispersion_pts", DEFAULTS["max_consensus_dispersion_pts"])
        )
        max_price = _to_float(params.get("max_entry_price", DEFAULTS["max_entry_price"]))
        hours_ahead = int(_to_float(params.get("hours_ahead", DEFAULTS["hours_ahead"])))
        sport_str = params.get("sport_keys", DEFAULTS["sport_keys"])
        if isinstance(sport_str, str):
            sport_keys = [s.strip() for s in sport_str.split(",") if s.strip()]
        else:
            sport_keys = list(sport_str)

        # 1. Get all recent odds snapshots (one row per outcome per bookmaker)
        all_snapshots = recent_odds_events(conn, sport_keys=sport_keys, hours_ahead=hours_ahead)
        if not all_snapshots:
            return []

        # 2. Group by canonical_event_hash
        events: Dict[str, List[dict]] = {}
        for snap in all_snapshots:
            eh = snap.get("canonical_event_hash", "")
            if eh:
                events.setdefault(eh, []).append(snap)

        signals = []
        events_with_books = 0
        events_with_matches = 0
        clob_fetches = 0

        for ev_hash, snapshots in events.items():
            # Get event metadata from first snapshot
            ref = snapshots[0]
            home_team = ref["home_team"]
            away_team = ref["away_team"]
            commence_time = ref["commence_time"]
            sport_key = ref["sport_key"]

            # 3. Reconstruct per-bookmaker binary pairs from individual outcome rows.
            #    odds_snapshots stores one row per (outcome_name, bookmaker_key).
            #    Group by bookmaker, find the home + away implied_prob_raw.
            book_outcomes: Dict[str, Dict[str, float]] = {}
            for s in snapshots:
                bk = s["bookmaker_key"]
                outcome_name = s.get("outcome_name", "")
                prob = _to_float(s.get("implied_prob_raw"))
                if prob <= 0:
                    continue
                book_outcomes.setdefault(bk, {})[outcome_name.lower()] = prob

            # 4. Compute no-vig fair probs per bookmaker (need both sides)
            home_fair_probs = []
            away_fair_probs = []
            for bk, outcomes in book_outcomes.items():
                home_raw = outcomes.get(home_team.lower(), 0.0)
                away_raw = outcomes.get(away_team.lower(), 0.0)
                if home_raw <= 0 or away_raw <= 0:
                    continue
                fh, fa = remove_vig_binary(home_raw, away_raw)
                home_fair_probs.append(fh)
                away_fair_probs.append(fa)

            effective_min_books = min_books_override.get(sport_key, min_books)
            if len(home_fair_probs) < effective_min_books:
                continue
            events_with_books += 1

            # 5. Compute consensus per side
            home_consensus = compute_consensus(home_fair_probs)
            away_consensus = compute_consensus(away_fair_probs)

            # 6. Find matching Polymarket markets
            matches = find_matching_polymarket_h2h_markets(
                conn, home_team, away_team, commence_time
            )
            if not matches:
                continue
            events_with_matches += 1

            for market in matches:
                cond_id = market["condition_id"]

                # 7. Map sportsbook teams to Polymarket outcome strings
                #    polymarket_market_outcomes now returns price from CLOB API
                #    fallback, so we can skip a separate fetch_live_clob_price call.
                poly_outcomes = polymarket_market_outcomes(conn, cond_id)
                clob_fetches += 1
                team_to_outcome = _map_teams_to_outcomes(
                    home_team, away_team, poly_outcomes
                )
                if not team_to_outcome:
                    continue

                # Build outcome→price lookup from CLOB response (if available)
                # and capture price snapshots for lead-lag analysis.
                outcome_prices: Dict[str, float] = {}
                for o in poly_outcomes:
                    p = _to_float(o.get("price"))
                    if p > 0:
                        outcome_prices[o["outcome"].lower()] = p
                        try:
                            from db import insert_poly_price_snapshot
                            insert_poly_price_snapshot(
                                conn,
                                condition_id=cond_id,
                                title=market.get("title", ""),
                                outcome=o["outcome"],
                                price=p,
                                market_type="h2h",
                                sport_key=sport_key,
                                event_hash=ev_hash,
                                commence_time=commence_time,
                            )
                        except Exception:
                            pass  # non-fatal

                # 8. Evaluate edge for each side
                for team, consensus in [
                    (home_team, home_consensus),
                    (away_team, away_consensus),
                ]:
                    outcome_label = team_to_outcome.get(team.lower())
                    if not outcome_label:
                        continue

                    # Per-side dispersion check
                    if consensus["dispersion"] > max_disp:
                        continue

                    # Use price from CLOB response if available, else fetch separately
                    poly_price = outcome_prices.get(outcome_label.lower())
                    if poly_price is None or poly_price <= 0:
                        poly_price = fetch_live_clob_price(cond_id, outcome_label)
                        clob_fetches += 1
                    if poly_price is None or poly_price <= 0:
                        continue

                    fair_prob = consensus["median"]
                    edge_pts = fair_prob - poly_price

                    # Persist all positive-edge observations for threshold analysis
                    if edge_pts > 0:
                        try:
                            from db import insert_edge_observation
                            insert_edge_observation(
                                conn,
                                instance_tag=INSTANCE_TAG,
                                sport_key=sport_key,
                                event_hash=ev_hash,
                                condition_id=cond_id,
                                title=market.get("title", ""),
                                team=team,
                                outcome_label=outcome_label,
                                poly_price=poly_price,
                                fair_prob=fair_prob,
                                edge_pts=edge_pts,
                                dispersion=consensus["dispersion"],
                                books_used=consensus["books_used"],
                                commence_time=commence_time,
                                met_threshold=edge_pts >= min_edge and poly_price <= max_price,
                            )
                        except Exception:
                            logger.debug("edge observation insert failed", exc_info=True)

                    if edge_pts < min_edge:
                        continue
                    if poly_price > max_price:
                        continue

                    # Annotation: insider convergence (never an entry gate)
                    convergence = {}
                    try:
                        convergence = insider_convergence_check(conn, cond_id)
                    except Exception:
                        logger.debug("insider_convergence_check failed for %s", cond_id, exc_info=True)

                    conf = compute_confidence_tier(edge_pts, consensus["books_used"])

                    signals.append(StrategySignal(
                        wallet=f"odds_{ev_hash}",
                        condition_id=cond_id,
                        outcome=outcome_label,
                        title=market.get("title", ""),
                        current_price=poly_price,
                        position_value=0.0,
                        analyzed_ts=_now_ts(),
                        metadata={
                            "canonical_event_hash": ev_hash,
                            "sport_key": sport_key,
                            "home_team": home_team,
                            "away_team": away_team,
                            "commence_time": commence_time,
                            "selected_team": team,
                            "selected_side": outcome_label.lower(),
                            "polymarket_price": poly_price,
                            "fair_prob": fair_prob,
                            "edge_pts": round(edge_pts, 4),
                            "books_used": consensus["books_used"],
                            "consensus_dispersion": consensus["dispersion"],
                            "insider_convergence": convergence,
                            "confidence_tier": conf["tier"],
                            "confidence_score": conf["confidence_score"],
                            "size_multiplier": conf["size_multiplier"],
                        },
                    ))

        logger.info(
            "sports discover: %d events, %d with books>=%d, %d with poly matches, "
            "%d CLOB fetches, %d signals",
            len(events), events_with_books, min_books, events_with_matches,
            clob_fetches, len(signals),
        )
        return signals

    def enter(
        self,
        conn,
        strategy: Dict[str, Any],
        signal: StrategySignal,
        open_positions: List[Dict[str, Any]],
    ) -> StrategyEntryDecision:
        params = _parse_params(strategy)
        meta = signal.metadata or {}

        # Gate 1: duplicate position
        for pos in open_positions:
            if (pos.get("condition_id") == signal.condition_id
                    and pos.get("outcome") == signal.outcome):
                return StrategyEntryDecision(
                    should_enter=False, reason="Duplicate open position"
                )

        # Gate 2: max concurrent positions
        max_concurrent = int(_to_float(
            params.get("max_concurrent_positions", DEFAULTS["max_concurrent_positions"])
        ))
        if len(open_positions) >= max_concurrent:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Max concurrent positions reached ({max_concurrent})",
            )

        # Gate 3: max exposure per market
        max_market_exp = _to_float(
            params.get("max_exposure_per_market_usdc", DEFAULTS["max_exposure_per_market_usdc"])
        )
        market_exposure = sum(
            _to_float(p.get("size_usdc", 0))
            for p in open_positions
            if p.get("condition_id") == signal.condition_id
        )
        bet_size = _to_float(params.get("bet_size_usdc", DEFAULTS["bet_size_usdc"]))
        if market_exposure + bet_size > max_market_exp:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Market exposure would exceed ${max_market_exp}",
            )

        # Gate 4 (max total instance exposure) is enforced by the shared
        # risk layer in strategy_runner.py via evaluate_entry_risk().

        # Gate 5: max entry price
        max_price = _to_float(params.get("max_entry_price", DEFAULTS["max_entry_price"]))
        if signal.current_price > max_price:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Entry price {signal.current_price:.2f} exceeds max {max_price}",
            )

        # Gate 6: event start buffer
        min_minutes = int(_to_float(
            params.get("min_minutes_before_start", DEFAULTS["min_minutes_before_start"])
        ))
        commence_str = meta.get("commence_time", "")
        if commence_str:
            try:
                commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                minutes_until = (commence_dt - datetime.now(timezone.utc)).total_seconds() / 60
                if minutes_until < min_minutes:
                    return StrategyEntryDecision(
                        should_enter=False,
                        reason=f"Event starts in {minutes_until:.0f}min (min {min_minutes})",
                    )
            except (ValueError, TypeError):
                pass

        # Gate 7: edge threshold still holds
        min_edge = _to_float(params.get("min_edge_pts", DEFAULTS["min_edge_pts"]))
        edge_pts = _to_float(meta.get("edge_pts"))
        if edge_pts < min_edge:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Edge {edge_pts:.3f} below threshold {min_edge}",
            )

        # Gate 8: min books (with optional per-sport override)
        min_books = int(_to_float(params.get("min_books", DEFAULTS["min_books"])))
        min_books_override = _parse_min_books_override(params)
        sport_key_signal = str(meta.get("sport_key", ""))
        effective_min_books = min_books_override.get(sport_key_signal, min_books)
        books_used = int(_to_float(meta.get("books_used", 0)))
        if books_used < effective_min_books:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Only {books_used} books (min {effective_min_books})",
            )

        # Gate 9: consensus dispersion
        max_disp = _to_float(
            params.get("max_consensus_dispersion_pts", DEFAULTS["max_consensus_dispersion_pts"])
        )
        disp = _to_float(meta.get("consensus_dispersion"))
        if disp > max_disp:
            return StrategyEntryDecision(
                should_enter=False,
                reason=f"Consensus dispersion {disp:.3f} exceeds max {max_disp}",
            )

        # Scale bet size by confidence tier
        size_mult = _to_float(meta.get("size_multiplier", 1.0), default=1.0)
        scaled_size = round(bet_size * size_mult, 2)
        conf_tier = meta.get("confidence_tier", "unknown")

        return StrategyEntryDecision(
            should_enter=True,
            reason=f"Edge {edge_pts:.3f} on {meta.get('selected_team', '')} [{conf_tier}]",
            size_usdc=scaled_size,
            entry_price=signal.current_price,
            execution_mode="paper",
            metadata=meta,
        )

    def manage(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
    ) -> StrategyManageState:
        cond_id = position.get("condition_id", "")
        outcome = position.get("outcome", "YES")

        # Fetch live Polymarket price (fall back to entry price on failure)
        live_price = fetch_live_clob_price(cond_id, outcome)
        entry_price = _to_float(
            position.get("avg_entry_price") or position.get("entry_price")
        )
        current_price = live_price if live_price is not None else entry_price

        # Check if market is still open
        market_open = True
        try:
            row = conn.execute(
                "SELECT is_open FROM market_metadata WHERE condition_id = ?",
                (cond_id,),
            ).fetchone()
            if row:
                market_open = bool(int(row["is_open"] or 0))
        except Exception:
            pass

        return StrategyManageState(
            current_price=current_price,
            signal_open=market_open,
            metadata={
                "market_open": market_open,
                "live_price_available": live_price is not None,
            },
        )

    def exit(
        self,
        conn,
        strategy: Dict[str, Any],
        position: Dict[str, Any],
        state: StrategyManageState,
    ) -> StrategyExitDecision:
        params = _parse_params(strategy)
        current_price = state.current_price
        entry_price = _to_float(position.get("avg_entry_price") or position.get("entry_price") or 0.0)

        # Parse plugin state
        plugin_state = position.get("plugin_state") or "{}"
        if isinstance(plugin_state, str):
            try:
                plugin_state = json.loads(plugin_state)
            except (json.JSONDecodeError, TypeError):
                plugin_state = {}

        # Exit 1: market resolved (use "closed" status — see spec semantic boundary)
        if current_price is not None:
            if current_price >= RESOLUTION_WIN or current_price <= RESOLUTION_LOSS:
                return StrategyExitDecision(
                    should_exit=True,
                    reason="market_resolved",
                    exit_price=current_price,
                    status="closed",
                )

        # Exit 2: market closed (not resolved but no longer open)
        if not state.signal_open and current_price is not None:
            if RESOLUTION_LOSS < current_price < RESOLUTION_WIN:
                return StrategyExitDecision(
                    should_exit=True,
                    reason="market_closed",
                    exit_price=current_price,
                    status="closed",
                )

        # Exit 3: convergence take-profit
        close_gap = _to_float(params.get("close_gap_pts", DEFAULTS["close_gap_pts"]))
        fair_prob = _to_float(plugin_state.get("fair_prob", 0))
        if current_price is not None and fair_prob > 0:
            gap = abs(fair_prob - current_price)
            in_profit = current_price > entry_price
            if gap <= close_gap and in_profit:
                return StrategyExitDecision(
                    should_exit=True,
                    reason="convergence_take_profit",
                    exit_price=current_price,
                    status="closed",
                )

        # Exit 4: stale/postponed
        grace_hours = _to_float(
            params.get("postponed_grace_hours", DEFAULTS["postponed_grace_hours"])
        )
        commence_str = plugin_state.get("commence_time", "")
        if commence_str:
            try:
                commence_dt = datetime.fromisoformat(commence_str.replace("Z", "+00:00"))
                hours_past = (
                    datetime.now(timezone.utc) - commence_dt
                ).total_seconds() / 3600
                if hours_past > grace_hours:
                    return StrategyExitDecision(
                        should_exit=True,
                        reason="stale_postponed",
                        exit_price=current_price or entry_price,
                        status="closed",
                    )
            except (ValueError, TypeError):
                pass

        return StrategyExitDecision(should_exit=False, reason="hold")

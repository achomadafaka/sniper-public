"""Stink Bid Runner — runtime loop for the tennis panic-dip paper trading system.

This is the ONLY code that places orders. It connects all the components:
- Task 1: DB tables (paper_limit_orders, paper_limit_fills, paper_limit_events)
- Task 2: feeds/tennis_scores.py (ESPN polling, MatchState, EspnOutageTracker)
- Task 3: feeds/polymarket_trade_tap.py (TradeTick, TradeTapState, run_trade_tap)
- Task 4: paper_limit_engine.py (PaperLimitEngine with all order lifecycle)
- Task 5: strategies/stink_bid_v1.py (tennis helpers)

Five concurrent responsibilities:
1. discover_and_place()  — Every 15 min: find qualifying tennis markets, place bids
2. poll_espn_and_gate()  — Every 60s: cancel bids when sets_completed >= 2
3. reprice_all()         — Every 15 min: adjust bids when favorite price moves
4. Trade tap fill detect — Continuous: _on_tick() checks fills for every trade
5. sync_watchlist()      — Every 30s: keep trade tap subscription current
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from sniper.feeds.polymarket_trade_tap import TradeTapState, TradeTick, run_trade_tap
from sniper.feeds.tennis_scores import (
    EspnOutageTracker,
    MatchState,
    fetch_scoreboard,
    find_match,
    get_sets_completed,
    parse_scoreboard,
)
from db import upsert_market_metadata
from sniper.services.paper_limit_engine import PaperLimitEngine, compute_bid_price
from sniper.markets.polymarket_api import fetch_market_by_condition
from sniper.strategies.stink_bid_v1 import (
    _extract_player_surnames,
    _extract_tour,
    _is_tennis_match_title,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default strategy parameters
# ---------------------------------------------------------------------------

_DEFAULT_PARAMS: dict[str, Any] = {
    "dip_pct": 0.30,
    "min_favorite": 0.55,
    "min_bid": 0.05,
    "min_fill_size": 50.0,
    "max_resting": 3,
    "max_daily_fills": 5,
    "reprice_threshold": 0.02,
    "min_volume_24h": 10000.0,
    "position_size_usdc": 25.0,
    "espn_pause_after": 3,
    "espn_cancel_after_min": 10,
}

# Map spec/design key names → canonical runner key names so DB params stored
# with either convention are accepted.
_PARAM_ALIASES: dict[str, str] = {
    "min_favorite_price": "min_favorite",
    "min_bid_price": "min_bid",
    "max_resting_orders": "max_resting",
    "max_position_size_usdc": "position_size_usdc",
}


def _normalize_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize parameter keys using _PARAM_ALIASES so both spec and runner
    key conventions are accepted."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        canonical = _PARAM_ALIASES.get(k, k)
        out[canonical] = v
    return out


_GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def _fetch_gamma_tennis_h2h(session: requests.Session) -> list[dict]:
    """Return {'event': e, 'market': m} dicts for active tennis H2H moneylines.

    Filters: tag 'tennis' AND title contains ' vs ' (drops outrights).
    Moneyline discriminator (validated 44/44 live events 2026-04-21):
      market.slug == event.slug
      AND market.question == event.title
      AND empty groupItemTitle
    """
    try:
        resp = session.get(
            _GAMMA_EVENTS_URL,
            params={
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": 500,
            },
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json()
    except Exception:
        logger.exception("stink_bid: gamma events fetch failed")
        return []

    pairs: list[dict] = []
    for e in events:
        tags = [
            t.get("slug", "") if isinstance(t, dict) else str(t)
            for t in (e.get("tags") or [])
        ]
        if "tennis" not in tags:
            continue
        title = (e.get("title") or "").strip()
        if " vs " not in title.lower():
            continue  # drop outrights
        event_slug = (e.get("slug") or "").strip()
        if not event_slug:
            continue
        for m in e.get("markets") or []:
            if (
                (m.get("slug") or "").strip() == event_slug
                and (m.get("question") or "").strip() == title
                and not (m.get("groupItemTitle") or "").strip()
            ):
                pairs.append({"event": e, "market": m})
                break
    return pairs


class StinkBidRunner:
    """Runtime orchestrator for the stink-bid tennis paper trading system.

    Args:
        db_path: Path to the SQLite database file.
        strategy_id: The strategy ID from the strategies table.
        params: Optional override dict for strategy parameters.
                Keys not provided fall back to _DEFAULT_PARAMS.
    """

    def __init__(
        self,
        db_path: str,
        strategy_id: int,
        params: dict[str, Any] | None = None,
    ) -> None:
        self._db_path = db_path
        self._strategy_id = strategy_id

        # Merge provided params over defaults (normalize aliases first)
        merged = dict(_DEFAULT_PARAMS)
        if params:
            merged.update(_normalize_params(params))

        self._dip_pct: float = float(merged["dip_pct"])
        self._min_favorite: float = float(merged["min_favorite"])
        self._min_bid: float = float(merged["min_bid"])
        self._min_fill_size: float = float(merged["min_fill_size"])
        self._max_resting: int = int(merged["max_resting"])
        self._max_daily_fills: int = int(merged["max_daily_fills"])
        self._reprice_threshold: float = float(merged["reprice_threshold"])
        self._min_volume_24h: float = float(merged["min_volume_24h"])
        self._position_size_usdc: float = float(merged["position_size_usdc"])

        # Shared state objects
        self._tap_state = TradeTapState()
        self._espn_pause_after = int(merged["espn_pause_after"])
        self._espn_cancel_after_min = int(merged["espn_cancel_after_min"])
        # Per-tour outage trackers — ATP failure shouldn't mask WTA outage
        self._outage_trackers: dict[str, EspnOutageTracker] = {}

        # Reusable HTTP session for Gamma API calls
        self._session = requests.Session()

    def _outage_tracker(self, tour: str) -> EspnOutageTracker:
        """Return the per-tour outage tracker, creating on first access."""
        if tour not in self._outage_trackers:
            self._outage_trackers[tour] = EspnOutageTracker(
                pause_after=self._espn_pause_after,
                cancel_after_min=self._espn_cancel_after_min,
            )
        return self._outage_trackers[tour]

    # ------------------------------------------------------------------
    # DB connection helper
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return a fresh connection (WAL mode, timeout=30, row_factory=Row)."""
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------------
    # Trade tap callback
    # ------------------------------------------------------------------

    def _on_tick(self, tick: TradeTick) -> None:
        """Check every resting order against the incoming trade tick.

        Called for every trade on watched tokens from the Polymarket CLOB
        WebSocket.  If a tick matches a resting order at or below the bid
        price, the engine's check_fill() handles the state transition.
        """
        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)
            resting = engine.get_resting_orders(strategy_id=self._strategy_id)
            for order in resting:
                if order["favorite_token_id"] != tick.token_id:
                    continue
                result = engine.check_fill(
                    order_id=order["order_id"],
                    tick=tick,
                    position_size_usdc=self._position_size_usdc,
                )
                if result == "strict":
                    logger.info(
                        "stink_bid fill_detected order_id=%d condition_id=%s "
                        "fill_type=strict price=%.4f size=%.2f",
                        order["order_id"],
                        order["condition_id"],
                        tick.price,
                        tick.size,
                    )
        except Exception:
            logger.exception("stink_bid _on_tick error token_id=%s", tick.token_id)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Watchlist sync
    # ------------------------------------------------------------------

    def sync_watchlist(self) -> None:
        """Sync the trade tap watched_tokens to match current resting orders.

        Adds tokens for new resting orders, removes tokens for orders that
        are no longer resting (filled, cancelled, expired).
        """
        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)
            resting = engine.get_resting_orders(strategy_id=self._strategy_id)
            resting_tokens = {o["favorite_token_id"] for o in resting}

            # Add new tokens
            for token_id in resting_tokens:
                if not self._tap_state.is_watched(token_id):
                    self._tap_state.watch(token_id)
                    logger.debug("watchlist add token_id=%s", token_id)

            # Remove stale tokens
            stale = self._tap_state.watched_tokens - resting_tokens
            for token_id in stale:
                self._tap_state.unwatch(token_id)
                logger.debug("watchlist remove token_id=%s", token_id)
        except Exception:
            logger.exception("stink_bid sync_watchlist error")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Gamma seed helper
    # ------------------------------------------------------------------

    def _seed_tennis_markets_from_gamma(
        self, conn: sqlite3.Connection, pairs: list[dict]
    ) -> tuple[int, set[str]]:
        """Upsert tennis H2H moneylines into market_metadata via db helper.

        Returns (rows_written, seeded_condition_ids).
        """
        seeded: set[str] = set()
        written = 0
        for pair in pairs:
            e = pair["event"]
            m = pair["market"]
            cid = (m.get("conditionId") or "").strip()
            if not cid:
                continue
            try:
                upsert_market_metadata(
                    conn,
                    cid,
                    title=str(e.get("title") or ""),
                    slug=str(m.get("slug") or ""),
                    event_slug=str(e.get("slug") or ""),
                    end_date_iso=str(e.get("endDate") or ""),
                    category="tennis",
                    category_source="gamma",
                    volume_24h=float(e.get("volume24hr") or 0.0),
                    is_open=1,
                )
                seeded.add(cid)
                written += 1
            except Exception:
                logger.exception("stink_bid seed upsert failed cond=%s", cid)
        if written:
            try:
                conn.commit()
            except Exception:
                logger.exception("stink_bid seed commit failed")
        return written, seeded

    # ------------------------------------------------------------------
    # Discovery and placement
    # ------------------------------------------------------------------

    def discover_and_place(self) -> None:
        """Scan market_metadata for qualifying tennis markets and place bids.

        Flow:
        1. Check ESPN outage tracker — skip if paused
        2. Query open markets from market_metadata
        3. For each: check tennis title, volume, existing order
        4. Resolve token IDs via Gamma API
        5. Determine favorite, check threshold
        6. Check ESPN match state — skip if sets >= 2 or not found
        7. Place order via paper limit engine
        """
        if any(t.should_pause_new_orders for t in self._outage_trackers.values()):
            logger.info(
                "stink_bid discover_and_place: skipping — ESPN outage tracker paused"
            )
            return

        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)

            # Cache parsed scoreboards per tour this cycle (None = ESPN down)
            scoreboards: dict[str, list[dict] | None] = {}

            # Per-cycle counters — emitted as a single INFO summary at end of cycle
            counters = {
                "scanned": 0,
                "tennis": 0,
                "placed": 0,
                "skipped_vol": 0,
                "skipped_existing": 0,
                "skipped_gamma": 0,
                "skipped_parse": 0,
                "skipped_players": 0,
                "skipped_fav": 0,
                "skipped_espn": 0,
                "skipped_live": 0,
                "skipped_lock": 0,
            }

            # Seed tennis H2H moneylines from Gamma event tags (authoritative)
            pairs = _fetch_gamma_tennis_h2h(self._session)
            written, seeded_cids = self._seed_tennis_markets_from_gamma(conn, pairs)
            logger.info(
                "stink_bid gamma_seed events=%d written=%d",
                len(pairs),
                written,
            )

            if not seeded_cids:
                rows = []
            else:
                placeholders = ",".join("?" * len(seeded_cids))
                rows = conn.execute(
                    f"""SELECT condition_id, title, slug, event_slug, volume_24h
                        FROM market_metadata
                        WHERE is_open = 1 AND condition_id IN ({placeholders})""",
                    tuple(seeded_cids),
                ).fetchall()

            for row in rows:
                counters["scanned"] += 1
                cond_id = row["condition_id"]
                title = row["title"]
                volume = float(row["volume_24h"] or 0)

                # --- Filter: tennis match title ---
                if not _is_tennis_match_title(title):
                    continue

                counters["tennis"] += 1

                # --- Filter: minimum volume ---
                if volume < self._min_volume_24h:
                    counters["skipped_vol"] += 1
                    engine.log_skip(
                        strategy_id=self._strategy_id,
                        condition_id=cond_id,
                        reason="below_volume",
                        details={"volume_24h": volume, "min": self._min_volume_24h},
                    )
                    continue

                # --- Filter: existing order ---
                if engine.has_order_for_match(self._strategy_id, cond_id):
                    counters["skipped_existing"] += 1
                    continue

                # --- Resolve token IDs via Gamma API ---
                try:
                    market = fetch_market_by_condition(self._session, cond_id)
                except Exception:
                    logger.exception(
                        "stink_bid fetch_market_by_condition failed cond=%s", cond_id
                    )
                    counters["skipped_gamma"] += 1
                    continue

                if not market:
                    # Gamma returned 200 with empty body for this condition_id.
                    # That is a definitive "this market no longer exists in the
                    # Polymarket index" signal, not a transient failure. If we
                    # leave market_metadata.is_open=1, this row will show up
                    # every 15 min and waste a Gamma call forever. Mark the row
                    # closed so the next cycle skips it. The upstream bug is in
                    # stream_low_buys.py meta_refresh — it evicts markets from
                    # the in-memory dict but never persists is_open=0 to SQLite.
                    # This is a runner-side self-heal; the streamer fix is the
                    # real cure.
                    counters["skipped_gamma"] += 1
                    logger.info(
                        "stink_bid gamma_market_missing cond=%s title=%r — "
                        "marking market_metadata is_open=0",
                        cond_id,
                        title,
                    )
                    try:
                        conn.execute(
                            "UPDATE market_metadata "
                            "SET is_open = 0, updated_at = CURRENT_TIMESTAMP "
                            "WHERE condition_id = ?",
                            (cond_id,),
                        )
                        conn.commit()
                    except Exception:
                        logger.exception(
                            "stink_bid gamma_market_missing: failed to mark "
                            "cond=%s closed", cond_id,
                        )
                    continue

                # Parse outcomes, clobTokenIds, outcomePrices
                try:
                    outcomes = json.loads(market.get("outcomes", "[]"))
                    token_ids = json.loads(market.get("clobTokenIds", "[]"))
                    prices_raw = json.loads(market.get("outcomePrices", "[]"))
                    prices = [float(p) for p in prices_raw]
                except (json.JSONDecodeError, ValueError, TypeError):
                    counters["skipped_parse"] += 1
                    logger.warning(
                        "stink_bid: unparseable Gamma data for cond=%s", cond_id
                    )
                    continue

                if len(outcomes) < 2 or len(token_ids) < 2 or len(prices) < 2:
                    counters["skipped_parse"] += 1
                    continue

                # --- Determine favorite (highest price outcome) ---
                fav_idx = 0
                for i in range(1, len(prices)):
                    if prices[i] > prices[fav_idx]:
                        fav_idx = i

                fav_price = prices[fav_idx]
                fav_token_id = token_ids[fav_idx]
                fav_label = outcomes[fav_idx]

                # --- Filter: minimum favorite price ---
                if fav_price < self._min_favorite:
                    counters["skipped_fav"] += 1
                    engine.log_skip(
                        strategy_id=self._strategy_id,
                        condition_id=cond_id,
                        reason="no_favorite_above_threshold",
                        details={
                            "favorite_price": fav_price,
                            "min_favorite": self._min_favorite,
                        },
                    )
                    continue

                # --- Extract player surnames and tour ---
                players = _extract_player_surnames(title)
                if not players:
                    counters["skipped_players"] += 1
                    logger.debug(
                        "stink_bid: cannot extract players from title=%r", title
                    )
                    continue
                player1, player2 = players
                primary_tour = _extract_tour(title)
                fallback_tour = "atp" if primary_tour == "wta" else "wta"

                # --- Check ESPN match state (try primary tour first, then fall back) ---
                entry = None
                actual_tour: Optional[str] = None
                for try_tour in (primary_tour, fallback_tour):
                    if try_tour not in scoreboards:
                        try:
                            sb_data = fetch_scoreboard(try_tour)
                        except Exception:
                            logger.exception(
                                "stink_bid fetch_scoreboard failed tour=%s", try_tour
                            )
                            sb_data = None

                        if sb_data is None:
                            self._outage_tracker(try_tour).record_failure()
                            logger.warning(
                                "stink_bid: ESPN fetch failed tour=%s "
                                "(consecutive=%d)",
                                try_tour,
                                self._outage_tracker(try_tour)._consecutive_failures,
                            )
                            scoreboards[try_tour] = None
                        else:
                            self._outage_tracker(try_tour).record_success()
                            scoreboards[try_tour] = parse_scoreboard(sb_data)

                    parsed = scoreboards[try_tour]
                    if parsed is None:
                        continue

                    candidate = find_match(parsed, player1, player2)
                    if candidate is not None:
                        entry = candidate
                        actual_tour = try_tour
                        break

                if entry is None:
                    counters["skipped_espn"] += 1
                    engine.log_skip(
                        strategy_id=self._strategy_id,
                        condition_id=cond_id,
                        reason="no_score_feed",
                        details={
                            "tour_primary": primary_tour,
                            "tour_fallback": fallback_tour,
                            "player1": player1,
                            "player2": player2,
                        },
                    )
                    continue

                match_state = get_sets_completed(entry["competition"])
                if match_state.sets_completed >= 2:
                    counters["skipped_live"] += 1
                    engine.log_skip(
                        strategy_id=self._strategy_id,
                        condition_id=cond_id,
                        reason="sets_completed_ge_2",
                        details={
                            "sets_completed": match_state.sets_completed,
                        },
                    )
                    continue

                # --- Place the order ---
                try:
                    order_id = engine.place_order(
                        strategy_id=self._strategy_id,
                        condition_id=cond_id,
                        market_title=title,
                        favorite_token_id=fav_token_id,
                        favorite_token_label=fav_label,
                        favorite_price=fav_price,
                        min_fill_size=self._min_fill_size,
                        dip_pct=self._dip_pct,
                        min_bid=self._min_bid,
                        event_slug=row["event_slug"] or "",
                        tour=actual_tour or primary_tour,
                        player1=player1,
                        player2=player2,
                        max_resting=self._max_resting,
                        max_daily_fills=self._max_daily_fills,
                    )
                except sqlite3.OperationalError as exc:
                    # DB lock contention — a primary-write failure on one
                    # market must not abort the rest of the cycle. Order is
                    # retriable: next cycle re-seeds this cond_id and retries.
                    counters["skipped_lock"] += 1
                    logger.warning(
                        "stink_bid place_order lock cond=%s favorite=%s: %s",
                        cond_id, fav_label, exc,
                    )
                    continue
                if order_id is not None:
                    counters["placed"] += 1
                    logger.info(
                        "stink_bid order_placed order_id=%d cond=%s "
                        "favorite=%s price=%.4f bid=%.4f",
                        order_id, cond_id, fav_label, fav_price,
                        compute_bid_price(fav_price, self._dip_pct, self._min_bid),
                    )

            logger.info(
                "stink_bid discover_cycle "
                "scanned=%(scanned)d tennis=%(tennis)d placed=%(placed)d "
                "skipped_vol=%(skipped_vol)d skipped_existing=%(skipped_existing)d "
                "skipped_gamma=%(skipped_gamma)d skipped_parse=%(skipped_parse)d "
                "skipped_players=%(skipped_players)d skipped_fav=%(skipped_fav)d "
                "skipped_espn=%(skipped_espn)d skipped_live=%(skipped_live)d "
                "skipped_lock=%(skipped_lock)d",
                counters,
            )
        except Exception:
            logger.exception("stink_bid discover_and_place error")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # ESPN gating
    # ------------------------------------------------------------------

    def poll_espn_and_gate(self) -> None:
        """Poll ESPN scoreboards and cancel orders when sets_completed >= 2.

        Also tracks ESPN outage state:
        - Record success/failure on each fetch
        - If should_cancel_all (prolonged outage): cancel all resting orders
        """
        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)
            resting = engine.get_resting_orders(strategy_id=self._strategy_id)

            if not resting:
                return

            # Group orders by tour for efficient scoreboard fetching
            tours: dict[str, list[dict]] = {}
            for order in resting:
                tour = order.get("tour", "atp") or "atp"
                tours.setdefault(tour, []).append(order)

            for tour, orders in tours.items():
                data = fetch_scoreboard(tour)
                if data is None:
                    self._outage_tracker(tour).record_failure()
                    logger.warning(
                        "stink_bid: ESPN fetch failed tour=%s "
                        "(consecutive=%d)",
                        tour, self._outage_tracker(tour)._consecutive_failures,
                    )
                    continue

                self._outage_tracker(tour).record_success()
                parsed = parse_scoreboard(data)

                for order in orders:
                    player1 = order.get("player1", "")
                    player2 = order.get("player2", "")
                    if not player1 or not player2:
                        continue

                    entry = find_match(parsed, player1, player2)
                    if entry is None:
                        # Match not on today's scoreboard — may not have started
                        continue

                    match_state = get_sets_completed(entry["competition"])
                    if match_state.sets_completed >= 2:
                        engine.cancel_order(
                            order_id=order["order_id"],
                            reason=f"sets_completed={match_state.sets_completed}",
                        )
                        logger.info(
                            "stink_bid gate_cancel order_id=%d cond=%s "
                            "sets_completed=%d",
                            order["order_id"],
                            order["condition_id"],
                            match_state.sets_completed,
                        )

            # Check for prolonged outage — cancel everything
            if any(t.should_cancel_all for t in self._outage_trackers.values()):
                # Re-fetch resting in case some were already cancelled above
                still_resting = engine.get_resting_orders(
                    strategy_id=self._strategy_id
                )
                for order in still_resting:
                    engine.cancel_order(
                        order_id=order["order_id"],
                        reason="espn_outage_prolonged",
                    )
                logger.warning(
                    "stink_bid: ESPN prolonged outage — cancelled %d orders",
                    len(still_resting),
                )
        except Exception:
            logger.exception("stink_bid poll_espn_and_gate error")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Repricing
    # ------------------------------------------------------------------

    def reprice_all(self) -> None:
        """Reprice all resting orders based on current market prices.

        For each resting order:
        1. Fetch current prices via Gamma API
        2. Find the favorite's current price by matching the outcome label
        3. If favorite flipped (price < 0.50) — cancel the order
        4. Otherwise reprice via engine.reprice_order()
        """
        conn = self._get_conn()
        try:
            engine = PaperLimitEngine(conn)
            resting = engine.get_resting_orders(strategy_id=self._strategy_id)

            for order in resting:
                cond_id = order["condition_id"]
                fav_label = order["favorite_token_label"]

                try:
                    market = fetch_market_by_condition(self._session, cond_id)
                except Exception:
                    logger.exception(
                        "stink_bid reprice fetch failed cond=%s", cond_id
                    )
                    continue

                if not market:
                    continue

                try:
                    outcomes = json.loads(market.get("outcomes", "[]"))
                    prices_raw = json.loads(market.get("outcomePrices", "[]"))
                    prices = [float(p) for p in prices_raw]
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue

                # Find the current price of the favorite outcome
                fav_price_now = None
                for i, label in enumerate(outcomes):
                    if label == fav_label and i < len(prices):
                        fav_price_now = prices[i]
                        break

                if fav_price_now is None:
                    logger.warning(
                        "stink_bid reprice: label %r not found in outcomes for cond=%s",
                        fav_label, cond_id,
                    )
                    continue

                # Check for favorite flip
                if fav_price_now < 0.50:
                    engine.cancel_order(
                        order_id=order["order_id"],
                        reason=f"favorite_flipped price={fav_price_now:.4f}",
                    )
                    logger.info(
                        "stink_bid favorite_flipped order_id=%d cond=%s "
                        "price=%.4f",
                        order["order_id"], cond_id, fav_price_now,
                    )
                    continue

                # Reprice
                engine.reprice_order(
                    order_id=order["order_id"],
                    new_favorite_price=fav_price_now,
                    threshold=self._reprice_threshold,
                    dip_pct=self._dip_pct,
                    min_bid=self._min_bid,
                )
        except Exception:
            logger.exception("stink_bid reprice_all error")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Main async loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main event loop — runs forever.

        Schedules:
        - Trade tap (continuous WebSocket)
        - sync_watchlist() every 30s
        - poll_espn_and_gate() every 60s
        - discover_and_place() + reprice_all() every 15 min
        """
        logger.info(
            "stink_bid_runner starting strategy_id=%d db=%s",
            self._strategy_id, self._db_path,
        )

        # Initial discovery before starting the loop
        self.discover_and_place()
        self.sync_watchlist()

        # Start trade tap in a background task
        tap_task = asyncio.create_task(
            run_trade_tap(
                state=self._tap_state,
                on_tick=self._on_tick,
            )
        )

        sync_interval = 30
        espn_interval = 60
        discover_interval = 900  # 15 min

        last_sync = 0.0
        last_espn = 0.0
        last_discover = 0.0

        try:
            while True:
                now = asyncio.get_running_loop().time()

                if now - last_sync >= sync_interval:
                    try:
                        self.sync_watchlist()
                    except Exception:
                        logger.exception("stink_bid sync_watchlist loop error")
                    last_sync = now

                if now - last_espn >= espn_interval:
                    try:
                        self.poll_espn_and_gate()
                    except Exception:
                        logger.exception("stink_bid poll_espn_and_gate loop error")
                    last_espn = now

                if now - last_discover >= discover_interval:
                    try:
                        self.discover_and_place()
                        self.reprice_all()
                    except Exception:
                        logger.exception(
                            "stink_bid discover/reprice loop error"
                        )
                    last_discover = now

                await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.info("stink_bid_runner shutting down")
        finally:
            tap_task.cancel()
            try:
                await tap_task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _load_strategy_params(db_path: str, strategy_id: int) -> dict[str, Any]:
    """Load strategy parameters from the strategies table.

    Returns the parsed JSON from ``strategies.parameters``, or an empty dict
    if the strategy row doesn't exist or has no parameters.
    """
    import db as sniper_db

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = sniper_db.get_strategy_by_id(conn, strategy_id)
    finally:
        conn.close()

    if row is None:
        logger.warning(
            "stink_bid: strategy_id=%d not found in DB — using defaults",
            strategy_id,
        )
        return {}

    raw = row["parameters"] if row["parameters"] else "{}"
    try:
        params = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning(
            "stink_bid: invalid JSON in strategies.parameters for id=%d — using defaults",
            strategy_id,
        )
        return {}

    if params:
        logger.info(
            "stink_bid: loaded %d params from DB for strategy_id=%d: %s",
            len(params), strategy_id, list(params.keys()),
        )
    return params


def main() -> None:
    """CLI entry point for running the stink bid runner as a service."""
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Stink Bid Runner")
    parser.add_argument("--db", default="sniper_tracker.db", help="DB path")
    parser.add_argument("--strategy-id", type=int, required=True,
                        help="Strategy ID from the strategies table")
    args = parser.parse_args()

    # Load params from DB, falling back to defaults for any missing keys
    db_params = _load_strategy_params(args.db, args.strategy_id)

    runner = StinkBidRunner(
        db_path=args.db,
        strategy_id=args.strategy_id,
        params=db_params,
    )
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()

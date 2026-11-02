import datetime as dt
import logging
import os
import time
from typing import Any, Dict, List

import requests
from flask import Blueprint, jsonify, request

from db import latest_redemption_success, record_redemption_attempt, redemption_success_exists, upsert_market_outcome
from sniper.markets.polymarket_api import build_session
from sniper.services.real_position_redeemer import redeem_condition_position
from sniper.routes.contexts import ApiFollowedContext
from sniper.runtime.wallet_key_loader import load_private_key


logger = logging.getLogger(__name__)
CLOB_BASE_URL = "https://clob.polymarket.com"
REDEEM_OUTCOME_SYNC_TIMEOUT_SECONDS = int(os.getenv("REDEEM_OUTCOME_SYNC_TIMEOUT_SECONDS", "25"))
REDEEM_WINNER_PRICE_MIN = float(os.getenv("REDEEM_WINNER_PRICE_MIN", "0.99"))
REDEEM_LOSER_PRICE_MAX = float(os.getenv("REDEEM_LOSER_PRICE_MAX", "0.01"))


def _updated_at() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fetch_clob_market_price(condition_id: str, outcome: str) -> float | None:
    """Fetch the live token price for a condition/outcome directly from the CLOB."""
    condition = str(condition_id or "").strip()
    normalized_outcome = str(outcome or "").strip().lower()
    if not condition or not normalized_outcome:
        return None
    # Synthetic IDs appear throughout tests and backfilled local rows. Only
    # hit the exchange for real Polymarket condition ids.
    if not (condition.startswith("0x") and len(condition) >= 10):
        return None
    try:
        resp = requests.get(f"{CLOB_BASE_URL}/markets/{condition}", timeout=5)
        if not resp.ok:
            return None
        tokens = resp.json().get("tokens", [])
        for token in tokens:
            if str(token.get("outcome") or "").strip().lower() != normalized_outcome:
                continue
            try:
                price = float(token.get("price") or 0.0)
            except (TypeError, ValueError):
                return None
            if price <= 0:
                return None
            return max(0.0, min(1.0, price))
    except Exception:
        return None
    return None


def create_api_followed_blueprint(ctx: ApiFollowedContext) -> Blueprint:
    bp = Blueprint("api_followed", __name__)
    conn = ctx["conn"]

    def _norm_outcome(value: Any) -> str:
        return str(value or "").strip().upper()

    def _enrich_with_resolution(
        items: List[Dict[str, Any]],
        *,
        condition_key: str = "condition_id",
        outcome_key: str = "outcome",
        prefer_payload_price: bool = False,
    ) -> List[Dict[str, Any]]:
        """Additive resolution enrichment for UI display.

        Never mutates existing `status` semantics. Adds:
        - is_resolved: bool
        - display_status: str
        - price_source: market_outcomes | position_signals | payload | clob | followed_positions | unknown
        """
        if not items:
            return items

        pairs = {
            (str(item.get(condition_key) or "").strip(), _norm_outcome(item.get(outcome_key)))
            for item in items
            if str(item.get(condition_key) or "").strip()
        }
        if not pairs:
            for item in items:
                item["is_resolved"] = False
                item["display_status"] = str(item.get("status") or "open")
                item["price_source"] = "unknown"
                item["current_price"] = None
            return items

        condition_ids = sorted({cid for cid, _ in pairs if cid})
        placeholders = ",".join("?" for _ in condition_ids)

        resolved_by_key: Dict[tuple[str, str], float] = {}
        resolved_by_condition: Dict[str, Dict[str, float]] = {}
        if condition_ids:
            for row in conn.execute(
                f"""
                SELECT condition_id, outcome, resolved_price
                FROM market_outcomes
                WHERE condition_id IN ({placeholders})
                """,
                condition_ids,
            ).fetchall():
                key = (str(row["condition_id"] or ""), _norm_outcome(row["outcome"]))
                try:
                    price = float(row["resolved_price"])
                except (TypeError, ValueError):
                    continue
                resolved_by_key[key] = price
                resolved_by_condition.setdefault(key[0], {})[key[1]] = price

        live_price_by_key: Dict[tuple[str, str], float] = {}
        if condition_ids:
            for row in conn.execute(
                f"""
                SELECT condition_id, outcome, current_price
                FROM position_signals
                WHERE condition_id IN ({placeholders})
                  AND is_open = 1
                ORDER BY analyzed_ts DESC
                """,
                condition_ids,
            ).fetchall():
                key = (str(row["condition_id"] or ""), _norm_outcome(row["outcome"]))
                if key in live_price_by_key:
                    continue
                try:
                    current_price = float(row["current_price"] or 0.0)
                except (TypeError, ValueError):
                    continue
                if current_price > 0:
                    live_price_by_key[key] = current_price

        for item in items:
            condition_id = str(item.get(condition_key) or "").strip()
            outcome = _norm_outcome(item.get(outcome_key))
            key = (condition_id, outcome)

            # Keep base status untouched for backend semantics.
            base_status = str(item.get("status") or "open")

            resolved_price = resolved_by_key.get(key)
            if resolved_price is not None:
                item["is_resolved"] = True
                item["display_status"] = "resolved"
                item["current_price"] = float(resolved_price)
                item["price_source"] = "market_outcomes"
                continue

            # Fallback for named-outcome markets: if item has YES/NO but
            # market_outcomes stores named tokens (e.g. 'rockets'/'hornets'),
            # resolve via condition-level lookup.
            #
            # Polymarket token structure: each condition_id has exactly YES+NO
            # tokens. For two-outcome markets (sports), YES maps to the primary
            # named token and NO maps to the complement. If the primary won
            # (resolved_price >= 0.97), YES→1.0 and NO→0.0, and vice versa.
            if outcome in ("YES", "NO"):
                cond_outcomes = resolved_by_condition.get(condition_id, {})
                if cond_outcomes and all(
                    p >= 0.97 or p <= 0.03 for p in cond_outcomes.values()
                ):
                    has_winner = any(p >= 0.97 for p in cond_outcomes.values())
                    fallback_price = (
                        (1.0 if has_winner else 0.0)
                        if outcome == "YES"
                        else (0.0 if has_winner else 1.0)
                    )
                    item["is_resolved"] = True
                    item["display_status"] = "resolved"
                    item["current_price"] = fallback_price
                    item["price_source"] = "market_outcomes_fallback"
                    continue

            item["is_resolved"] = False
            item["display_status"] = base_status

            # Some callers inject an authoritative current_price (e.g. direct CLOB
            # marks for real positions). Preserve that before falling back to
            # generic position_signals rows from unrelated wallets.
            try:
                payload_price = float(item.get("current_price") or 0.0)
            except (TypeError, ValueError):
                payload_price = 0.0
            if prefer_payload_price and payload_price > 0:
                if not item.get("price_source"):
                    item["price_source"] = "payload"
                continue

            live_price = live_price_by_key.get(key)
            if live_price is not None:
                item["current_price"] = float(live_price)
                item["price_source"] = "position_signals"
                continue

            # Respect an existing positive payload price if available.
            if payload_price > 0:
                item["current_price"] = payload_price
                item["price_source"] = "payload"
            else:
                item["current_price"] = None
                item["price_source"] = "unknown"

        return items

    def _env_flag(name: str, default: bool) -> bool:
        raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
        return raw in {"1", "true", "yes", "on"}

    def _resolved_price_for(condition_id: str, outcome: str) -> float | None:
        row = conn.execute(
            """
            SELECT resolved_price
            FROM market_outcomes
            WHERE condition_id = ?
              AND LOWER(COALESCE(outcome, '')) = ?
            ORDER BY resolved_at DESC
            LIMIT 1
            """,
            (condition_id, outcome.lower()),
        ).fetchone()
        if row is not None:
            try:
                return float(row["resolved_price"])
            except (TypeError, ValueError):
                pass

        # Fallback for named-outcome markets (YES/NO vs named tokens).
        norm = outcome.strip().upper()
        if norm in ("YES", "NO"):
            rows = conn.execute(
                """
                SELECT outcome, resolved_price
                FROM market_outcomes
                WHERE condition_id = ?
                """,
                (condition_id,),
            ).fetchall()
            if rows:
                prices = []
                for r in rows:
                    try:
                        prices.append(float(r["resolved_price"]))
                    except (TypeError, ValueError):
                        prices.append(0.5)
                if all(p >= 0.97 or p <= 0.03 for p in prices):
                    has_winner = any(p >= 0.97 for p in prices)
                    if norm == "YES":
                        return 1.0 if has_winner else 0.0
                    else:
                        return 0.0 if has_winner else 1.0

        return None

    def _parse_any_ts(value: Any) -> int:
        if value is None:
            return int(time.time())
        if isinstance(value, (int, float)):
            ivalue = int(value)
            if ivalue > 10_000_000_000:
                return ivalue // 1000
            return ivalue
        text = str(value).strip()
        if not text:
            return int(time.time())
        try:
            ivalue = int(float(text))
            if ivalue > 10_000_000_000:
                return ivalue // 1000
            return ivalue
        except Exception:
            pass
        text = text.replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return int(parsed.timestamp())
        except Exception:
            return int(time.time())

    def _sync_market_outcome_for_condition(condition_id: str) -> int:
        """Best-effort one-shot outcome sync for a specific condition.

        Returns number of outcome rows upserted.
        """
        cid = str(condition_id or "").strip().lower()
        if not cid:
            return 0
        session = build_session()
        try:
            resp = session.get(
                f"{CLOB_BASE_URL}/markets/{cid}",
                timeout=REDEEM_OUTCOME_SYNC_TIMEOUT_SECONDS,
            )
            if int(resp.status_code or 0) == 404:
                return 0
            resp.raise_for_status()
            payload = resp.json() if resp.content else {}
            if not isinstance(payload, dict) or not bool(payload.get("closed")):
                return 0
            tokens = payload.get("tokens")
            if not isinstance(tokens, list) or not tokens:
                return 0

            resolved_at = _parse_any_ts(
                payload.get("closed_time")
                or payload.get("closedTime")
                or payload.get("end_date_iso")
                or payload.get("endDate")
            )
            upserted = 0
            for token in tokens:
                if not isinstance(token, dict):
                    continue
                outcome = str(token.get("outcome") or "").strip().lower()
                if not outcome:
                    continue
                winner = token.get("winner")
                if isinstance(winner, bool):
                    resolved_price = 1.0 if winner else 0.0
                else:
                    try:
                        price = float(token.get("price") or 0.0)
                    except Exception:
                        continue
                    if price >= REDEEM_WINNER_PRICE_MIN:
                        resolved_price = 1.0
                    elif price <= REDEEM_LOSER_PRICE_MAX:
                        resolved_price = 0.0
                    else:
                        continue
                upsert_market_outcome(
                    conn,
                    cid,
                    outcome,
                    resolved_price=resolved_price,
                    resolved_at=resolved_at,
                    source="redeem_condition_sync",
                )
                upserted += 1
            if upserted:
                conn.commit()
            return upserted
        finally:
            try:
                session.close()
            except Exception:
                pass

    def _portfolio_payload(wallet: str) -> Dict[str, Any]:
        rows = ctx["followed_positions"](
            conn,
            wallet=wallet,
            include_auto=True,
            limit=500,
        )
        positions: List[Dict[str, Any]] = []
        for row in rows:
            entry_price = float(row["entry_price"] or 0.0)
            current_price = float(row["current_price"] or entry_price or 0.0)
            current_value = float(row["current_amount"] or row["entry_notional"] or 0.0)
            title = str(row["title"] or "")
            positions.append(
                {
                    "condition_id": str(row["condition_id"] or ""),
                    "title": title,
                    "outcome": str(row["outcome"] or ""),
                    "avgPrice": entry_price,
                    "curPrice": current_price,
                    "currentValue": current_value,
                }
            )
        return {
            "wallet": wallet,
            "count": len(positions),
            "positions": positions,
            "updated_at": _updated_at(),
        }

    @bp.get("/api/followed-trades")
    def api_followed_trades():
        wallet = ctx["normalize_wallet"](request.args.get("wallet"))
        ignored = ctx["ignored_wallets"](ctx["conn"])
        force = str(request.args.get("force") or "").strip().lower() in {"1", "true", "yes"}
        sync_queued = False
        if wallet and not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if wallet and wallet in ignored:
            return jsonify(
                {
                    "count": 0,
                    "updated_at": _updated_at(),
                    "items": [],
                    "force_requested": force,
                    "sync_queued": False,
                }
            )

        if wallet:
            if force:
                sync_queued = ctx["enqueue_background_sync"](
                    f"followed-trades:{wallet}",
                    ctx["background_sync_followed_trades"],
                    wallet,
                )
            else:
                ctx["sync_followed_wallet"](wallet, force=False)
            rows = ctx["followed_trades"](ctx["conn"], limit=ctx["MAX_FOLLOWED_UI_TRADES"], wallet=wallet)
        else:
            if force:
                sync_queued = ctx["enqueue_background_sync"](
                    "followed-trades:*", ctx["background_sync_followed_trades"], None
                )
            else:
                ctx["sync_followed_trades"](force=False)
            rows = ctx["followed_trades"](ctx["conn"], limit=ctx["MAX_FOLLOWED_UI_TRADES"])

        label_by_wallet = {
            str(row["wallet"] or ""): str(row["label"] or "") for row in ctx["watchlist_wallets"](ctx["conn"])
        }
        deleted_hashes = ctx["deleted_trade_hashes"](
            ctx["conn"],
            (str(row["transaction_hash"] or "") for row in rows),
        )
        data: List[Dict[str, Any]] = []
        for row in rows:
            if ctx["normalize_wallet"](row["wallet"]) in ignored:
                continue
            tx_hash = ctx["normalize_tx_hash"](row["transaction_hash"])
            if tx_hash and tx_hash in deleted_hashes:
                continue
            entry_notional = float(row["entry_notional"] or 0.0)
            if entry_notional < ctx["MIN_UI_ORDER_NOTIONAL"]:
                continue
            wallet_key = str(row["wallet"] or "")
            data.append(
                {
                    "wallet": wallet_key,
                    "wallet_label": label_by_wallet.get(wallet_key, ""),
                    "transaction_hash": row["transaction_hash"],
                    "condition_id": row["condition_id"],
                    "side": row["side"],
                    "outcome": row["outcome"],
                    "bet_direction": ctx["bet_direction"](row["outcome"]),
                    "title": row["title"],
                    "event_slug": row["event_slug"],
                    "entry_price": row["entry_price"],
                    "entry_size": row["entry_size"],
                    "entry_notional": entry_notional,
                    "entry_ts": row["entry_ts"],
                    "entry_at": ctx["format_unix"](row["entry_ts"]),
                    "status": row["status"],
                    "status_label": row["status_label"],
                    "status_reason": row["status_reason"],
                    "is_closed": bool(int(row["is_closed"] or 0)),
                    "is_win": None if row["is_win"] is None else bool(int(row["is_win"])),
                    "exited_early": None if row["exited_early"] is None else bool(int(row["exited_early"])),
                    "live_price": row["live_price"],
                    "exit_price": row["exit_price"],
                    "display_price": row["live_price"] if row["live_price"] is not None else row["exit_price"],
                    "current_value": row["current_value"],
                    "realized_pnl": row["realized_pnl"],
                    "close_ts": row["close_ts"],
                    "close_at": ctx["format_unix"](row["close_ts"]),
                    "event_end_ts": row["event_end_ts"],
                    "event_end_at": ctx["format_unix"](row["event_end_ts"]),
                    "added_ts": row["added_ts"],
                    "added_at": ctx["format_unix"](row["added_ts"]),
                    "last_synced_ts": row["last_synced_ts"],
                    "last_synced_at": ctx["format_unix"](row["last_synced_ts"]),
                }
            )

        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "force_requested": force,
                "sync_queued": sync_queued,
                "items": data,
            }
        )

    # Compatibility endpoints for legacy UI builds that still use
    # /api/followed-wallets naming. Canonical store remains watchlist_wallets.
    @bp.post("/api/followed-wallets")
    def api_followed_wallets_add():
        payload = request.get_json(silent=True) or {}
        wallet = ctx["normalize_wallet"](payload.get("wallet"))
        label = str(payload.get("label") or "").strip()
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if wallet in ctx["ignored_wallets"](ctx["conn"]):
            return jsonify({"error": "wallet is ignored; unignore it first"}), 409

        if "upsert_watchlist_wallet" in ctx:
            ctx["upsert_watchlist_wallet"](ctx["conn"], wallet, label)
            if "sync_watchlist_wallet" in ctx:
                ctx["sync_watchlist_wallet"](wallet, force=True)
        else:
            ts = int(dt.datetime.now(dt.UTC).timestamp())
            ctx["conn"].execute(
                """
                INSERT INTO watchlist_wallets (wallet, label, added_ts, last_synced_ts)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(wallet) DO UPDATE SET label=excluded.label
                """,
                (wallet, label, ts),
            )
            ctx["conn"].commit()

        return jsonify({"ok": True, "wallet": wallet})

    @bp.get("/api/followed-wallets")
    def api_followed_wallets_list():
        # Compatibility endpoint for legacy dashboard code paths.
        rows = ctx["watchlist_wallets"](ctx["conn"])
        ignored = ctx["ignored_wallets"](ctx["conn"])
        wallets = [ctx["normalize_wallet"](row["wallet"]) for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets) if wallets else {}
        # Bulk-lookup user_analysis for scoring data.
        analysis_map: Dict[str, Any] = {}
        profile_map: Dict[str, int] = {}
        pos_agg_map: Dict[str, Dict[str, Any]] = {}
        last_active_map: Dict[str, int] = {}
        if wallets:
            wallets_lower = [w.lower() for w in wallets]
            ph = ",".join("?" for _ in wallets_lower)
            for arow in conn.execute(
                f"""SELECT LOWER(wallet) as wallet, classification, insider_score,
                           speculator_score, win_rate, closed_sample_size,
                           total_closed_count,
                           closed_realized_pnl, open_cash_pnl, pnl
                    FROM user_analysis WHERE LOWER(wallet) IN ({ph})""",
                wallets_lower,
            ).fetchall():
                analysis_map[str(arow["wallet"])] = arow

            # Profile prediction count from wallet_profiles.
            for prow in conn.execute(
                f"""SELECT LOWER(wallet) as wallet, profile_prediction_count
                    FROM wallet_profiles WHERE LOWER(wallet) IN ({ph})""",
                wallets_lower,
            ).fetchall():
                cnt = prow["profile_prediction_count"]
                profile_map[str(prow["wallet"])] = int(cnt) if cnt is not None else 0

            # Position aggregates from followed_positions (manual-follow scope).
            for agg in conn.execute(
                f"""SELECT LOWER(wallet) as wallet,
                           COUNT(*) as cnt,
                           SUM(
                             CASE WHEN cash_pnl IS NOT NULL THEN cash_pnl
                                  WHEN current_amount IS NOT NULL AND entry_notional IS NOT NULL
                                       THEN current_amount - entry_notional
                                  ELSE 0 END
                           ) as unrealized
                    FROM followed_positions
                    WHERE LOWER(wallet) IN ({ph})
                      AND added_source = 'manual'
                    GROUP BY LOWER(wallet)""",
                wallets_lower,
            ).fetchall():
                pos_agg_map[str(agg["wallet"])] = {
                    "current_open_position_count": int(agg["cnt"] or 0),
                    "open_unrealized_pnl": float(agg["unrealized"] or 0.0),
                }

            # Last active timestamp from position_signals.
            for la_row in conn.execute(
                f"""SELECT LOWER(wallet) as wallet, MAX(latest_trade_ts) as last_ts
                    FROM position_signals
                    WHERE LOWER(wallet) IN ({ph})
                    GROUP BY LOWER(wallet)""",
                wallets_lower,
            ).fetchall():
                ts = la_row["last_ts"]
                if ts:
                    last_active_map[str(la_row["wallet"])] = int(ts)

        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet = ctx["normalize_wallet"](row["wallet"])
            if wallet in ignored:
                continue
            added_ts = int(row["added_ts"] or 0)
            last_synced_ts = int(row["last_synced_ts"] or 0)
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet) if wallet else {
                "username": None,
                "pseudonym": None,
                "profile_image": None,
            }
            username = identity.get("username")
            pseudonym = identity.get("pseudonym")
            display_name = username or pseudonym or str(row["label"] or "") or wallet
            la_ts = last_active_map.get(wallet.lower(), 0)
            last_active_ts = la_ts if la_ts > 0 else last_synced_ts
            pa = pos_agg_map.get(wallet.lower(), {})
            entry: Dict[str, Any] = {
                "wallet": wallet,
                "label": str(row["label"] or ""),
                "username": username,
                "pseudonym": pseudonym,
                "profile_image": identity.get("profile_image"),
                "display_name": display_name,
                "added_ts": added_ts,
                "added_at": ctx["format_unix"](added_ts),
                "last_synced_ts": last_synced_ts,
                "last_synced_at": ctx["format_unix"](last_synced_ts),
                "last_active_ts": last_active_ts,
                "last_active_at": ctx["format_unix"](last_active_ts),
                "profile_prediction_count": profile_map.get(wallet.lower()),
                "activity_count_source": "wallet_profiles.profile_prediction_count",
                "current_open_position_count": pa.get("current_open_position_count", 0),
                "open_unrealized_pnl": pa.get("open_unrealized_pnl", 0.0),
                "open_pnl_source": "followed_positions",
            }
            a = analysis_map.get(wallet.lower()) if wallet else None
            if a:
                entry["classification"] = a["classification"]
                entry["insider_score"] = a["insider_score"]
                entry["speculator_score"] = a["speculator_score"]
                entry["win_rate"] = a["win_rate"]
                entry["closed_sample_size"] = a["closed_sample_size"]
                entry["total_closed_count"] = a["total_closed_count"]
                raw_crp = a["closed_realized_pnl"]
                raw_ocp = a["open_cash_pnl"]
                raw_pnl = a["pnl"]
                # Distinguish pre-backfill default (both 0, but combined pnl != 0)
                # from genuinely zero realized PnL.
                crp = float(raw_crp) if raw_crp is not None else 0.0
                ocp = float(raw_ocp) if raw_ocp is not None else 0.0
                combined = float(raw_pnl) if raw_pnl is not None else 0.0
                if crp == 0.0 and ocp == 0.0 and combined != 0.0:
                    # Pre-backfill: columns have default 0 but real pnl exists
                    entry["closed_realized_pnl"] = None
                else:
                    entry["closed_realized_pnl"] = crp
            else:
                entry["closed_realized_pnl"] = None
            entry["wallet_pnl_source"] = "user_analysis.closed_realized_pnl"
            data.append(entry)
        return jsonify({"count": len(data), "updated_at": _updated_at(), "items": data})

    @bp.delete("/api/followed-wallets/<wallet>")
    def api_followed_wallets_remove(wallet: str):
        wallet = ctx["normalize_wallet"](wallet)
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400

        if "remove_watchlist_wallet" in ctx:
            ctx["remove_watchlist_wallet"](ctx["conn"], wallet)
        else:
            ctx["conn"].execute("DELETE FROM watchlist_wallets WHERE wallet=?", (wallet,))
            ctx["conn"].commit()

        return jsonify({"ok": True, "wallet": wallet})

    @bp.get("/api/followed-wallets/<wallet>/portfolio")
    def api_followed_wallet_portfolio(wallet: str):
        wallet = ctx["normalize_wallet"](wallet)
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        return jsonify(_portfolio_payload(wallet))

    @bp.get("/api/wallet-portfolio/<wallet>")
    def api_wallet_portfolio_compat(wallet: str):
        # Backward-compatible alias used by older Positions tab builds.
        wallet = ctx["normalize_wallet"](wallet)
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        return jsonify(_portfolio_payload(wallet))

    @bp.get("/api/real-trades")
    def api_real_trades():
        default_wallet = str(os.getenv("REAL_TRADES_DEFAULT_WALLET", "")).strip()
        wallet = ctx["normalize_wallet"](request.args.get("wallet") or default_wallet)
        if not wallet:
            return jsonify({"open": [], "closed": [], "redeemable": [], "settled_loss": [], "stats": {}})
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        limit = ctx["safe_int"](request.args.get("limit"), 5000)
        if limit <= 0:
            limit = 5000
        limit = min(limit, 20000)
        payload = ctx["real_trades_positions_summary"](
            conn,
            wallet=wallet,
            limit=limit,
        )
        open_items = [dict(item, status="open") for item in (payload.get("open") or [])]
        closed_items = [dict(item, status="closed") for item in (payload.get("closed") or [])]

        # Real-trades summary rows may not include current_price.
        # Inject best-effort current price from followed_positions for this wallet
        # before resolution enrichment runs.
        condition_ids = sorted(
            {
                str(item.get("condition_id") or "").strip()
                for item in open_items
                if str(item.get("condition_id") or "").strip()
            }
        )
        if condition_ids:
            placeholders = ",".join("?" for _ in condition_ids)
            price_rows = conn.execute(
                f"""
                SELECT condition_id, outcome, current_price
                FROM followed_positions
                WHERE LOWER(wallet) = ?
                  AND condition_id IN ({placeholders})
                  AND current_price > 0
                """,
                [wallet, *condition_ids],
            ).fetchall()
            price_map: Dict[tuple[str, str], float] = {}
            for row in price_rows:
                key = (str(row["condition_id"] or "").strip(), _norm_outcome(row["outcome"]))
                try:
                    price_map[key] = float(row["current_price"] or 0.0)
                except (TypeError, ValueError):
                    continue
            for item in open_items:
                existing = item.get("current_price")
                try:
                    existing_value = float(existing or 0.0)
                except (TypeError, ValueError):
                    existing_value = 0.0
                if existing_value > 0:
                    continue
                key = (
                    str(item.get("condition_id") or "").strip(),
                    _norm_outcome(item.get("outcome")),
                )
                mapped = price_map.get(key)
                if mapped and mapped > 0:
                    item["current_price"] = mapped
                    item["price_source"] = "followed_positions"

        for item in open_items:
            if str(item.get("condition_id") or "").strip() == "":
                continue
            clob_price = _fetch_clob_market_price(
                str(item.get("condition_id") or "").strip(),
                str(item.get("outcome") or "").strip(),
            )
            if clob_price is None:
                continue
            item["current_price"] = clob_price
            item["price_source"] = "clob"

        # Pre-fetch missing market outcomes for open positions (best-effort).
        for item in open_items:
            cid = str(item.get("condition_id") or "").strip()
            if not cid:
                continue
            existing = conn.execute(
                "SELECT 1 FROM market_outcomes WHERE condition_id = ?", (cid,)
            ).fetchone()
            if not existing:
                try:
                    _sync_market_outcome_for_condition(cid)
                except Exception:
                    pass

        _enrich_with_resolution(open_items, prefer_payload_price=True)
        _enrich_with_resolution(closed_items)

        # Split into four truthful buckets: open / redeemable / settled_loss / closed.
        redeem_threshold = max(0.0, min(1.0, ctx["safe_float"](
            os.getenv("REDEEM_MIN_RESOLVED_PRICE", "0.97")
        )))
        truly_open: List[Dict[str, Any]] = []
        redeemable_items: List[Dict[str, Any]] = []
        settled_loss_items: List[Dict[str, Any]] = []
        for item in open_items:
            if item.get("is_resolved"):
                item["status"] = "resolved"
                settlement = item.get("current_price")
                size = item.get("size_shares", 0) or 0
                spent = item.get("spent", 0) or 0
                claim_value = (settlement * size) if settlement is not None and size > 0 else None
                item["settlement_price"] = settlement
                item["claim_value"] = claim_value
                item["claim_pnl"] = (claim_value - spent) if claim_value is not None else None
                item["claim_pnl_pct"] = (
                    (item["claim_pnl"] / spent) * 100
                    if item.get("claim_pnl") is not None and spent > 0
                    else None
                )
                item["resolution_source"] = item.get("price_source", "unknown")
                # Classify claimability based on settlement vs redeem threshold.
                if settlement is not None and settlement >= redeem_threshold:
                    # Check if already redeemed — move to closed if so.
                    cid = str(item.get("condition_id") or "").strip().lower()
                    outcome_key = str(item.get("outcome") or "").strip().lower()
                    redemption = latest_redemption_success(
                        conn, wallet=wallet, condition_id=cid, outcome=outcome_key,
                    )
                    if redemption is not None:
                        received = (
                            redemption["amount_claimed"]
                            if redemption["amount_claimed"] is not None
                            else claim_value
                        )
                        realized_pnl = (received - spent) if received is not None else None
                        pct_pnl = (
                            (realized_pnl / spent) * 100
                            if realized_pnl is not None and spent > 0
                            else None
                        )
                        item["status"] = "closed"
                        item["close_type"] = "redeemed"
                        item["closed_source"] = "redemption_log"
                        item["exit_price"] = settlement
                        item["received"] = received
                        item["realized_pnl"] = realized_pnl
                        item["pct_pnl"] = pct_pnl
                        item["tx_hash"] = redemption["tx_hash"]
                        item["redeemed_at_ts"] = redemption["attempted_ts"]
                        item["redeemed_at"] = ctx["format_unix"](redemption["attempted_ts"])
                        item["last_traded_at"] = redemption["attempted_ts"]
                        item["can_redeem"] = False
                        item["redeem_state"] = "redeemed"
                        item["redeem_reason"] = "Already redeemed"
                        closed_items.append(item)
                    else:
                        item["can_redeem"] = True
                        item["redeem_state"] = "claimable"
                        item["redeem_reason"] = "Claimable winning position"
                        redeemable_items.append(item)
                else:
                    item["can_redeem"] = False
                    item["redeem_state"] = "settled_loss"
                    item["redeem_reason"] = "Resolved against your position"
                    settled_loss_items.append(item)
            else:
                truly_open.append(item)
        open_items = truly_open
        resolved_items = redeemable_items + settled_loss_items

        # Enrich closed items with explicit exit fields (skip redeemed rows already enriched).
        for item in closed_items:
            if item.get("close_type") == "redeemed":
                continue
            item["exit_price"] = item.get("avg_exit")
            item["realized_pnl"] = item.get("pnl")
            if item.get("exit_price") is not None:
                item["current_price"] = item.get("exit_price")
                item["price_source"] = "exit_price"

        summary = payload.get("summary") or {}
        summary["open_count"] = len(open_items)
        summary["redeemable_count"] = len(redeemable_items)
        summary["settled_loss_count"] = len(settled_loss_items)
        summary["resolved_count"] = len(resolved_items)
        summary["closed_count"] = len(closed_items)

        # Recompute net_pnl across all buckets so the summary is truthful.
        recomputed_pnl = 0.0
        for item in open_items:
            cp = item.get("cash_pnl")
            if cp is not None:
                recomputed_pnl += float(cp)
        for item in resolved_items:
            cp = item.get("claim_pnl")
            if cp is not None:
                recomputed_pnl += float(cp)
        for item in closed_items:
            rp = item.get("realized_pnl") or item.get("pnl")
            if rp is not None:
                recomputed_pnl += float(rp)
        summary["net_pnl"] = recomputed_pnl

        payload["open"] = open_items
        payload["redeemable"] = redeemable_items
        payload["settled_loss"] = settled_loss_items
        payload["resolved"] = resolved_items
        payload["closed"] = closed_items
        payload["summary"] = summary
        payload["updated_at"] = _updated_at()
        return jsonify(payload)

    @bp.post("/api/real-positions/redeem")
    def api_real_positions_redeem():
        payload = request.get_json(silent=True) or {}
        default_wallet = str(os.getenv("REAL_TRADES_DEFAULT_WALLET", "")).strip()
        wallet = ctx["normalize_wallet"](payload.get("wallet") or default_wallet)
        condition_id = str(payload.get("condition_id") or "").strip().lower()
        outcome = str(payload.get("outcome") or "").strip().lower()
        if wallet and not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if not condition_id or not outcome:
            return jsonify({"error": "condition_id and outcome are required"}), 400

        min_resolved_price = max(0.0, min(1.0, ctx["safe_float"](os.getenv("REDEEM_MIN_RESOLVED_PRICE", "0.97"))))
        payload_summary = ctx["real_trades_positions_summary"](conn, wallet=wallet, limit=20000)
        open_items = payload_summary.get("open") or []
        open_match = next(
            (
                row
                for row in open_items
                if str(row.get("condition_id") or "").strip().lower() == condition_id
                and str(row.get("outcome") or "").strip().lower() == outcome
            ),
            None,
        )
        # Source path from real_trades_positions_summary (real_trades or my_trades).
        position_source = "real_trade_history"
        if open_match is None:
            # Fallback to followed_positions: users can have tracked positions
            # synced from wallet history without local real_trades rows.
            followed_rows = ctx["followed_positions"](conn, wallet=wallet, include_auto=True, limit=5000)
            followed_match = next(
                (
                    row
                    for row in followed_rows
                    if str(row["condition_id"] or "").strip().lower() == condition_id
                    and str(row["outcome"] or "").strip().lower() == outcome
                    and float(row["current_size"] or 0.0) > 0.0
                ),
                None,
            )
            if followed_match is None:
                return jsonify(
                    {
                        "ok": False,
                        "error": "No redeemable open position found for wallet/condition/outcome",
                    }
                ), 404
            position_source = "followed_positions"
            open_match = {
                "trades": 0,
                "spent": float(followed_match["entry_notional"] or 0.0),
                "avg_entry": float(followed_match["entry_price"] or 0.0),
                "size_shares": float(followed_match["current_size"] or 0.0),
            }

        resolved_price = _resolved_price_for(condition_id, outcome)
        sync_attempted = False
        synced_rows = 0
        if resolved_price is None and _env_flag("REDEEM_AUTO_SYNC_OUTCOME", True):
            sync_attempted = True
            synced_rows = _sync_market_outcome_for_condition(condition_id)
            if synced_rows > 0:
                resolved_price = _resolved_price_for(condition_id, outcome)
        if resolved_price is None:
            return jsonify(
                {
                    "ok": False,
                    "code": "outcome_unresolved",
                    "error": "Market outcome is not resolved yet.",
                    "sync_attempted": sync_attempted,
                    "synced_rows": synced_rows,
                }
            ), 409
        if resolved_price < min_resolved_price:
            return jsonify(
                {
                    "ok": False,
                    "code": "not_redeemable_loss",
                    "error": "This market resolved against your position; there is nothing to redeem.",
                    "resolved_price": resolved_price,
                    "threshold_price": min_resolved_price,
                }
            ), 409

        if redemption_success_exists(
            conn,
            wallet=wallet,
            condition_id=condition_id,
            outcome=outcome,
        ):
            return jsonify(
                {
                    "ok": True,
                    "already_redeemed": True,
                    "wallet": wallet,
                    "condition_id": condition_id,
                    "outcome": outcome.upper(),
                    "message": "Redemption already completed for this position.",
                }
            )

        dry_run = _env_flag("REDEEM_DRY_RUN", True)
        armed = _env_flag("REDEEM_ARMED", False)
        live_mode = (not dry_run) and armed
        attempt_mode = "live" if live_mode else "dry_run"
        profile_url = f"https://polymarket.com/profile/{wallet or default_wallet}"

        metadata = {
            "wallet": wallet,
            "condition_id": condition_id,
            "outcome": outcome,
            "open_trade_count": int(open_match.get("trades") or 0),
            "entry_spent": float(open_match.get("spent") or 0.0),
            "avg_entry": float(open_match.get("avg_entry") or 0.0),
            "position_source": position_source,
        }

        if not live_mode:
            log_id = record_redemption_attempt(
                conn,
                wallet=wallet,
                condition_id=condition_id,
                outcome=outcome,
                attempt_mode="dry_run",
                status="dry_run",
                resolved_price=resolved_price,
                threshold_price=min_resolved_price,
                metadata=metadata,
            )
            conn.commit()
            return jsonify(
                {
                    "ok": True,
                    "dry_run": True,
                    "armed": armed,
                    "wallet": wallet,
                    "condition_id": condition_id,
                    "outcome": outcome.upper(),
                    "resolved_price": resolved_price,
                    "threshold_price": min_resolved_price,
                    "position_source": position_source,
                    "log_id": log_id,
                    "message": "Dry-run only. Set REDEEM_DRY_RUN=0 and REDEEM_ARMED=1 for live redemption.",
                    "url": profile_url,
                }
            )

        try:
            key_meta = load_private_key(expected_wallet=wallet)
            result = redeem_condition_position(
                wallet=wallet,
                condition_id=condition_id,
                outcome=outcome,
                private_key=str(key_meta["private_key"]),
                wait_for_receipt=True,
            )
            receipt_status = int(result.get("receipt_status", 1))
            tx_hash = str(result.get("tx_hash") or "").strip()
            status = "success" if receipt_status == 1 and tx_hash else "failed"
            log_id = record_redemption_attempt(
                conn,
                wallet=wallet,
                condition_id=condition_id,
                outcome=outcome,
                attempt_mode=attempt_mode,
                status=status,
                resolved_price=resolved_price,
                threshold_price=min_resolved_price,
                tx_hash=tx_hash,
                metadata={**metadata, "receipt_status": receipt_status},
            )
            conn.commit()
            if status != "success":
                return jsonify(
                    {
                        "ok": False,
                        "error": "Redemption transaction failed on-chain",
                        "wallet": wallet,
                        "condition_id": condition_id,
                        "outcome": outcome.upper(),
                        "tx_hash": tx_hash,
                        "log_id": log_id,
                    }
                ), 502
            return jsonify(
                {
                    "ok": True,
                    "dry_run": False,
                    "armed": armed,
                    "wallet": wallet,
                    "condition_id": condition_id,
                    "outcome": outcome.upper(),
                    "resolved_price": resolved_price,
                    "threshold_price": min_resolved_price,
                    "position_source": position_source,
                    "tx_hash": tx_hash,
                    "log_id": log_id,
                    "message": "Redemption transaction submitted.",
                }
            )
        except Exception as exc:
            logger.exception("Real redemption failed for %s %s %s", wallet, condition_id, outcome)
            log_id = record_redemption_attempt(
                conn,
                wallet=wallet,
                condition_id=condition_id,
                outcome=outcome,
                attempt_mode=attempt_mode,
                status="failed",
                resolved_price=resolved_price,
                threshold_price=min_resolved_price,
                error=str(exc),
                metadata=metadata,
            )
            conn.commit()
            return jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "wallet": wallet,
                    "condition_id": condition_id,
                    "outcome": outcome.upper(),
                    "log_id": log_id,
                    "url": profile_url,
                }
            ), 500

    @bp.post("/api/execute-real-trade")
    def api_execute_real_trade():
        """Execute a real trade on Polymarket via the CLOB API.

        Requires REAL_TRADING_ENABLED=true and a valid wallet key file.
        """
        payload = request.get_json(silent=True) or {}
        condition_id = str(payload.get("condition_id") or "").strip()
        outcome = str(payload.get("outcome") or "").strip()
        size_usdc = ctx["safe_float"](payload.get("size_usdc"))
        # Optional: source wallet and metadata for exit-safety lineage tracking
        source_wallet = str(payload.get("wallet") or "").strip()
        market_title = str(payload.get("market_title") or "").strip()
        source_size = ctx["safe_float"](payload.get("source_size"))

        if not condition_id:
            return jsonify({"error": "condition_id is required"}), 400
        if not outcome:
            return jsonify({"error": "outcome is required"}), 400
        if size_usdc <= 0:
            return jsonify({"error": "size_usdc must be > 0"}), 400

        enabled = str(os.getenv("REAL_TRADING_ENABLED", "false")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not enabled:
            return jsonify({
                "error": "Real trading disabled. Set REAL_TRADING_ENABLED=true in .env to enable.",
                "enabled": False,
            }), 501

        # Validate wallet key
        try:
            from sniper.runtime.wallet_key_loader import load_private_key
            load_private_key(expected_wallet=None)
        except Exception as e:
            return jsonify({
                "error": f"Wallet key not configured: {e}",
                "enabled": True,
                "configured": False,
            }), 501

        try:
            from sniper.services.real_trader import RealTrader
            import json as _json

            trader = RealTrader(dry_run=False)

            if not trader.clob:
                return jsonify({
                    "error": "CLOB client failed to connect — check logs",
                }), 500

            # Fetch current market price
            current_price = trader._fetch_live_price(condition_id, outcome)
            if current_price is None:
                return jsonify({
                    "error": "Cannot fetch live market price for this market/outcome",
                }), 400

            if current_price <= 0 or current_price >= 1.0:
                return jsonify({
                    "error": f"Market price {current_price:.3f} is outside tradeable range (0, 1)",
                }), 400

            # Check for duplicate open position
            existing = trader.conn.execute(
                "SELECT trade_id FROM real_trades WHERE condition_id = ? AND UPPER(outcome) = UPPER(?) AND status = 'open' LIMIT 1",
                (condition_id, outcome),
            ).fetchone()
            if existing:
                return jsonify({
                    "error": f"Already have an open real position for this market/outcome (trade #{existing['trade_id']})",
                }), 409

            # Place the order
            result = trader.place_order(
                condition_id, outcome, size_usdc, current_price, side='BUY',
            )

            if not result:
                return jsonify({
                    "error": "Order placement failed — check logs for details",
                }), 500

            order_id = result.get('orderID') or result.get('_order_id', '')
            token_id = result.get('_token_id', '')
            size_shares = result.get('_shares') or (size_usdc / current_price if current_price > 0 else 0)

            # Resolve signer wallet (our wallet, not the source's)
            try:
                from sniper.runtime.wallet_key_loader import load_private_key as _load_pk
                signer_wallet = _load_pk(expected_wallet=None)["signer"]
            except Exception:
                signer_wallet = "manual"

            # Record trade in real_trades with source lineage for exit safety
            metadata = {
                "source": "manual_ui",
                "source_wallet": source_wallet,
                "source_size": source_size if source_size > 0 else None,
            }
            trader.conn.execute('''
                INSERT INTO real_trades
                (wallet, market_title, condition_id, outcome, direction,
                 entry_price, executed_price, size_usd, size_shares, token_id,
                 status, opened_at, notes, order_id, dry_run)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', datetime('now'), ?, ?, 0)
            ''', (
                signer_wallet,
                market_title,
                condition_id,
                outcome,
                outcome,
                current_price,
                current_price,
                size_usdc,
                float(size_shares or 0),
                str(token_id or ''),
                _json.dumps(metadata),
                str(order_id or ''),
            ))
            trader.conn.commit()

            trade_id = trader.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

            return jsonify({
                "ok": True,
                "trade_id": trade_id,
                "condition_id": condition_id,
                "outcome": outcome,
                "size_usdc": size_usdc,
                "entry_price": current_price,
                "order_id": order_id,
                "message": f"Real trade placed: {outcome} ${size_usdc:.2f} @ ${current_price:.3f}",
            }), 200

        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({
                "error": f"Failed to execute trade: {str(e)}",
            }), 500

    @bp.get("/api/followed-positions")
    def api_followed_positions():
        wallet = ctx["normalize_wallet"](request.args.get("wallet"))
        if wallet and not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400

        # Default to manual followlist only for UI consistency.
        # Auto-followed positions can still be requested explicitly via include_auto=1.
        include_auto = str(request.args.get("include_auto") or "false").strip().lower() in {"1", "true", "yes"}
        force = str(request.args.get("force") or "").strip().lower() in {"1", "true", "yes"}
        sync_queued = False
        limit = ctx["safe_int"](request.args.get("limit"), ctx["MAX_FOLLOWED_POSITION_UI_ROWS"])
        if limit <= 0:
            limit = ctx["MAX_FOLLOWED_POSITION_UI_ROWS"]
        limit = min(limit, 5000)

        ignored = ctx["ignored_wallets"](conn)
        if wallet and wallet in ignored:
            return jsonify(
                {
                    "count": 0,
                    "updated_at": _updated_at(),
                    "items": [],
                    "force_requested": force,
                    "sync_queued": False,
                }
            )

        if wallet:
            if force:
                sync_queued = ctx["enqueue_background_sync"](
                    f"followed-positions:{wallet}",
                    ctx["background_sync_followed_positions"],
                    wallet,
                )
            else:
                ctx["sync_followed_positions_wallet"](wallet, force=False)
            rows = ctx["followed_positions"](conn, wallet=wallet, include_auto=include_auto, limit=limit)
        else:
            # Always enqueue sync as background task — never block the HTTP response
            ctx["enqueue_background_sync"](
                "followed-positions:*",
                ctx["background_sync_followed_positions"],
                None,
            )
            sync_queued = True
            rows = ctx["followed_positions"](conn, include_auto=include_auto, limit=limit)

        rows = [row for row in rows if ctx["normalize_wallet"](row["wallet"]) not in ignored]
        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](conn, wallets)

        analysis_by_wallet: Dict[str, Any] = {}
        if wallets:
            placeholders = ",".join("?" for _ in wallets)
            for analysis_row in conn.execute(
                f"""
                SELECT
                    wallet,
                    insider_score,
                    speculator_score,
                    classification,
                    tier,
                    strategy_group,
                    strategy_confidence,
                    position_count,
                    win_rate,
                    closed_sample_size,
                    account_age_days,
                    is_fresh_account
                FROM user_analysis
                WHERE wallet IN ({placeholders})
                """,
                wallets,
            ).fetchall():
                analysis_by_wallet[str(analysis_row["wallet"] or "")] = analysis_row

        signal_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        if wallets:
            placeholders = ",".join("?" for _ in wallets)
            for signal_row in conn.execute(
                f"""
                SELECT *
                FROM position_signals
                WHERE wallet IN ({placeholders})
                  AND is_open = 1
                ORDER BY analyzed_ts DESC
                """,
                wallets,
            ).fetchall():
                key = ctx["followed_position_key"](
                    str(signal_row["wallet"] or ""),
                    str(signal_row["condition_id"] or ""),
                    str(signal_row["outcome"] or ""),
                )
                if key in signal_by_key:
                    continue
                signal_by_key[key] = dict(signal_row)

        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet_key = str(row["wallet"] or "")
            identity = ctx["wallet_identity"](wallet_key, profiles_by_wallet)
            analysis = analysis_by_wallet.get(wallet_key)
            key = ctx["followed_position_key"](
                wallet_key, str(row["condition_id"] or ""), str(row["outcome"] or "")
            )
            signal = signal_by_key.get(key, {})
            position_title = str(signal.get("title") or row["title"] or "")
            event_slug = str(signal.get("event_slug") or row["event_slug"] or "")
            added_source = str(row["added_source"] or "manual")
            signal_position_value = float(signal.get("position_value") or 0.0)
            followed_current_amount = float(row["current_amount"] or 0.0)
            position_value = signal_position_value if signal_position_value > 0 else followed_current_amount
            entry_price = float(signal.get("entry_price") or 0.0)
            if entry_price <= 0:
                entry_price = float(row["entry_price"] or 0.0)
            current_price = float(signal.get("current_price") or 0.0)
            if current_price <= 0:
                current_price = float(row["current_price"] or 0.0)
            # If current_price is still <= 0 after fallback, set to None instead of 0
            if current_price <= 0:
                current_price = None
            entry_notional = float(signal.get("entry_notional") or 0.0)
            if entry_notional <= 0:
                entry_notional = float(row["entry_notional"] or 0.0)
            current_size = float(signal.get("current_size") or 0.0)
            if current_size <= 0:
                current_size = float(row["current_size"] or 0.0)

            item = {
                "wallet": wallet_key,
                "username": identity["username"],
                "pseudonym": identity["pseudonym"],
                "profile_image": identity["profile_image"],
                "condition_id": str(row["condition_id"] or ""),
                "outcome": str(row["outcome"] or ""),
                "title": position_title,
                "event_slug": event_slug,
                "label": str(row["label"] or ""),
                "status": "open",
                "added_source": added_source,
                "is_auto_followed": added_source == "auto_relevant",
                "added_ts": int(row["added_ts"] or 0),
                "added_at": ctx["format_unix"](row["added_ts"]),
                "last_synced_ts": int(row["last_synced_ts"] or 0),
                "last_synced_at": ctx["format_unix"](row["last_synced_ts"]),
                "position_classification": str(signal.get("position_classification") or "position_low_signal"),
                "position_insider_score": float(signal.get("position_insider_score") or 0.0),
                "position_speculator_score": float(signal.get("position_speculator_score") or 0.0),
                "position_value": float(position_value),
                "position_share": float(signal.get("position_share") or 0.0),
                "position_count_wallet": int(signal.get("position_count_wallet") or 0),
                "entry_anchor_ts": int(signal.get("entry_anchor_ts") or 0),
                "entry_anchor_at": ctx["format_unix"](signal.get("entry_anchor_ts")),
                "entry_price": float(entry_price),
                "entry_size": float(signal.get("entry_size") or 0.0),
                "entry_notional": float(entry_notional),
                "current_price": float(current_price) if current_price is not None else None,
                "current_size": float(current_size),
                # current_amount = actual current market value of the position
                # = shares held × current price (NOT same as position_value which is entry notional)
                "current_amount": float(current_size * current_price) if current_size > 0 and current_price is not None and current_price > 0 else float(position_value),
                # unrealized_pnl: use cashPnl from Polymarket directly (most accurate).
                # Falls back to manual computation only if cashPnl was not synced yet.
                # cashPnl = currentValue - initialValue (Polymarket already computes this correctly).
                "unrealized_pnl": (
                    float(row["cash_pnl"]) if row["cash_pnl"] is not None
                    else (float((current_size * current_price) - entry_notional) if current_size > 0 and current_price is not None and current_price > 0 and entry_notional > 0 else None)
                ),
                "realized_pnl": float(row["realized_pnl"]) if row["realized_pnl"] is not None else None,
                "unrealized_pnl_pct": float(((current_price - entry_price) / entry_price) * 100) if entry_price > 0 and current_price is not None and current_price > 0 else None,
                "latest_trade_ts": int(signal.get("latest_trade_ts") or 0),
                "latest_trade_at": ctx["format_unix"](signal.get("latest_trade_ts")),
                "buys_5m": int(signal.get("buys_5m") or 0),
                "buys_1h": int(signal.get("buys_1h") or 0),
                "buys_24h": int(signal.get("buys_24h") or 0),
                "trades_5m": int(signal.get("trades_5m") or 0),
                "trades_1h": int(signal.get("trades_1h") or 0),
                "trades_24h": int(signal.get("trades_24h") or 0),
                "sells_5m": int(signal.get("sells_5m") or 0),
                "sells_1h": int(signal.get("sells_1h") or 0),
                "sells_24h": int(signal.get("sells_24h") or 0),
                "notional_5m": float(signal.get("notional_5m") or 0.0),
                "notional_1h": float(signal.get("notional_1h") or 0.0),
                "notional_24h": float(signal.get("notional_24h") or 0.0),
                "sell_notional_5m": float(signal.get("sell_notional_5m") or 0.0),
                "sell_notional_1h": float(signal.get("sell_notional_1h") or 0.0),
                "sell_notional_24h": float(signal.get("sell_notional_24h") or 0.0),
                "buys_since_entry": int(signal.get("buys_since_entry") or 0),
                "sells_since_entry": int(signal.get("sells_since_entry") or 0),
                "buy_notional_since_entry": float(signal.get("buy_notional_since_entry") or 0.0),
                "sell_notional_since_entry": float(signal.get("sell_notional_since_entry") or 0.0),
                "net_notional_24h": float(signal.get("net_notional_24h") or 0.0),
                "net_notional_since_entry": float(signal.get("net_notional_since_entry") or 0.0),
                "last_trade_side": str(signal.get("last_trade_side") or ""),
                "activity_state": str(signal.get("activity_state") or "holding"),
                "hold_hours": (lambda h: None if h is None or float(h) < 0 else float(h))(signal.get("hold_hours")),
                "unique_sizes_5m": int(signal.get("unique_sizes_5m") or 0),
                "max_trades_60s": int(signal.get("max_trades_60s") or 0),
                "signal_reason": str(signal.get("signal_reason") or ""),
                "analyzed_ts": int(signal.get("analyzed_ts") or 0),
                "analyzed_at": ctx["format_unix"](signal.get("analyzed_ts")),
                "wallet_insider_score": float(analysis["insider_score"] or 0.0) if analysis else 0.0,
                "wallet_speculator_score": float(analysis["speculator_score"] or 0.0) if analysis else 0.0,
                "wallet_classification": str(analysis["classification"] or "") if analysis else "",
                "wallet_tier": str(analysis["tier"] or "") if analysis else "",
                "wallet_strategy_group": str(analysis["strategy_group"] or "") if analysis else "",
                "wallet_strategy_confidence": float(analysis["strategy_confidence"] or 0.0) if analysis else 0.0,
                "wallet_position_count": int(analysis["position_count"] or 0) if analysis else 0,
                "win_rate": float(analysis["win_rate"] or 0.0) if analysis else 0.0,
                "closed_sample_size": int(analysis["closed_sample_size"] or 0) if analysis else 0,
                "account_age_days": float(analysis["account_age_days"] or 99999.0) if analysis else 99999.0,
                "is_fresh_account": bool(int(analysis["is_fresh_account"] or 0)) if analysis else False,
                # Shared aliases for sorting/filtering compatibility.
                "classification": str(signal.get("position_classification") or "position_low_signal"),
                "insider_score": float(signal.get("position_insider_score") or 0.0),
                "speculator_score": float(signal.get("position_speculator_score") or 0.0),
                "tier": str(analysis["tier"] or "") if analysis else "",
                "strategy_group": str(analysis["strategy_group"] or "") if analysis else "",
            }
            data.append(item)

        _enrich_with_resolution(data)

        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "force_requested": force,
                "sync_queued": sync_queued,
                "items": data,
            }
        )

    @bp.post("/api/followed-positions")
    def api_followed_positions_add():
        payload = request.get_json(silent=True) or {}
        wallet = ctx["normalize_wallet"](payload.get("wallet"))
        condition_id = str(payload.get("condition_id") or "").strip().lower()
        outcome = str(payload.get("outcome") or "").strip().lower()
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if wallet in ctx["ignored_wallets"](ctx["conn"]):
            return jsonify({"error": "wallet is ignored; unignore it first"}), 409
        if not condition_id:
            return jsonify({"error": "condition_id is required"}), 400

        ctx["upsert_followed_position"](
            ctx["conn"],
            wallet=wallet,
            condition_id=condition_id,
            outcome=outcome,
            title=str(payload.get("title") or ""),
            event_slug=str(payload.get("event_slug") or ""),
            label=str(payload.get("label") or ""),
            added_source="manual",
        )
        ctx["sync_followed_positions_wallet"](wallet, force=True)
        return jsonify(
            {
                "ok": True,
                "wallet": wallet,
                "condition_id": condition_id,
                "outcome": outcome,
            }
        )

    @bp.delete("/api/followed-positions/<wallet>/<condition_id>/<outcome>")
    def api_followed_positions_remove(wallet: str, condition_id: str, outcome: str):
        wallet = ctx["normalize_wallet"](wallet)
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if not str(condition_id or "").strip():
            return jsonify({"error": "condition_id is required"}), 400
        ctx["remove_followed_position"](
            ctx["conn"],
            wallet=wallet,
            condition_id=condition_id,
            outcome=outcome,
        )
        return jsonify(
            {
                "ok": True,
                "wallet": wallet,
                "condition_id": str(condition_id or "").strip().lower(),
                "outcome": str(outcome or "").strip().lower(),
            }
        )

    @bp.post("/api/followed-trades")
    def api_followed_trades_add():
        payload = request.get_json(silent=True) or {}
        wallet = ctx["normalize_wallet"](payload.get("wallet"))
        tx_hash = ctx["normalize_tx_hash"](payload.get("transaction_hash"))
        condition_id = str(payload.get("condition_id") or "").strip()

        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if wallet in ctx["ignored_wallets"](ctx["conn"]):
            return jsonify({"error": "wallet is ignored; unignore it first"}), 409
        if not tx_hash:
            return jsonify({"error": "transaction_hash is required"}), 400
        if not condition_id:
            return jsonify({"error": "condition_id is required"}), 400
        if ctx["is_trade_deleted"](ctx["conn"], tx_hash):
            return jsonify({"error": "trade is deleted/tombstoned"}), 409

        ctx["upsert_followed_trade"](
            ctx["conn"],
            wallet=wallet,
            transaction_hash=tx_hash,
            condition_id=condition_id,
            side=str(payload.get("side") or ""),
            outcome=str(payload.get("outcome") or ""),
            title=str(payload.get("title") or ""),
            event_slug=str(payload.get("event_slug") or ""),
            entry_price=ctx["safe_float"](payload.get("entry_price")),
            entry_size=ctx["safe_float"](payload.get("entry_size")),
            entry_ts=ctx["safe_int"](payload.get("entry_ts")),
        )
        ctx["sync_followed_wallet"](wallet, force=True)
        return jsonify({"ok": True, "wallet": wallet, "transaction_hash": tx_hash})

    @bp.delete("/api/followed-trades/unfollow/<wallet>/<path:tx_hash>")
    def api_followed_trades_unfollow(wallet: str, tx_hash: str):
        wallet = ctx["normalize_wallet"](wallet)
        tx_hash = ctx["normalize_tx_hash"](tx_hash)
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if not tx_hash:
            return jsonify({"error": "transaction_hash is required"}), 400
        ctx["remove_followed_trade"](ctx["conn"], wallet, tx_hash)
        return jsonify({"ok": True, "wallet": wallet, "transaction_hash": tx_hash})

    @bp.delete("/api/followed-trades/<wallet>/<path:tx_hash>")
    def api_followed_trades_remove(wallet: str, tx_hash: str):
        _ = wallet
        result = ctx["delete_trade_and_tombstone"](tx_hash, source="/api/followed-trades")
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "delete failed")}), int(result.get("status_code", 400))
        return jsonify(result)

    @bp.post("/api/real-positions/close")
    def api_real_positions_close():
        """Close a real position by selling tokens on-chain."""
        # Validate wallet key is available (same loader as real_trader.py)
        try:
            from sniper.runtime.wallet_key_loader import load_private_key
            load_private_key(expected_wallet=None)
        except Exception as e:
            return jsonify({
                "error": f"Wallet key not configured: {e}",
                "configured": False,
            }), 501

        data = request.get_json() or {}
        condition_id = data.get("condition_id", "").strip()
        outcome = data.get("outcome", "").strip()

        if not condition_id or not outcome:
            return jsonify({"error": "condition_id and outcome are required"}), 400

        try:
            from sniper.services.real_trader import RealTrader

            trader = RealTrader(dry_run=False)
            c = trader.conn.cursor()
            c.execute(
                "SELECT * FROM real_trades WHERE condition_id = ? AND UPPER(outcome) = UPPER(?) AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
                (condition_id, outcome),
            )
            row = c.fetchone()

            if not row:
                return jsonify({
                    "error": f"No open position found for condition_id={condition_id}, outcome={outcome}",
                }), 404

            trade_id = dict(row)['trade_id']

            # Fetch live market price
            exit_price = trader._fetch_live_price(condition_id, outcome)
            if exit_price is None:
                # Fallback to position_signals current_price
                sig = trader.conn.execute(
                    "SELECT current_price FROM position_signals WHERE condition_id = ? AND outcome = ? ORDER BY updated_at DESC LIMIT 1",
                    (condition_id, outcome),
                ).fetchone()
                if sig and sig['current_price']:
                    exit_price = float(sig['current_price'])

            if exit_price is None or exit_price <= 0:
                return jsonify({
                    "error": "Unable to determine exit price — no live market data available",
                }), 400

            trader.close_position(trade_id, exit_price, reason="manual_close_via_ui")

            return jsonify({
                "ok": True,
                "condition_id": condition_id,
                "outcome": outcome,
                "trade_id": trade_id,
                "exit_price": exit_price,
                "message": "Position close submitted",
            }), 200
        except Exception as e:
            return jsonify({
                "error": f"Failed to close position: {str(e)}",
            }), 500

    @bp.post("/api/real-orders/cancel")
    def api_real_orders_cancel():
        """Cancel an unfilled LIVE order on the CLOB."""
        data = request.get_json() or {}
        trade_id = data.get("trade_id")
        if not trade_id:
            return jsonify({"error": "trade_id is required"}), 400

        try:
            from sniper.services.real_trader import RealTrader
            trader = RealTrader(dry_run=False)
            success = trader.cancel_order(int(trade_id), reason="manual_cancel_via_ui")
            if success:
                return jsonify({"ok": True, "trade_id": trade_id, "message": "Order cancelled"}), 200
            return jsonify({"error": "Cancel failed — check logs"}), 500
        except Exception as e:
            return jsonify({"error": f"Failed to cancel order: {str(e)}"}), 500

    return bp

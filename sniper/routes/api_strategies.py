import datetime as dt
import json
import logging
import requests
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request
from db import (
    ensure_paper_trades_duplicate_of_column,
    ensure_strategy_positions_live_price_columns,
    get_followed_wallets,
    set_followed_wallet_status,
    upsert_followed_wallet_record,
)
from sniper.routes.contexts import ApiStrategiesContext
from sniper.services.strategy_service import (
    VALID_STRATEGY_TYPES,
    normalize_wallet,
    validate_wallet,
    validate_strategy_type,
    validate_parameters,
)

logger = logging.getLogger(__name__)


def _updated_at() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _strategy_runtime(conn, strategy_id: int, *, strategy_type: str = "", wallet: str = "") -> Dict[str, Any]:
    # For insider_copy strategies, use paper_trades as the source of truth
    if strategy_type == "insider_copy_v1" and wallet:
        return _strategy_runtime_from_paper_trades(conn, wallet)

    ensure_strategy_positions_live_price_columns(conn)
    last_run = conn.execute(
        """
        SELECT *
        FROM strategy_runs
        WHERE strategy_id = ?
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (strategy_id,),
    ).fetchone()
    positions = conn.execute(
        """
        SELECT *
        FROM strategy_positions
        WHERE strategy_id = ?
        ORDER BY updated_ts DESC, position_id DESC
        """,
        (str(strategy_id),),
    ).fetchall()

    open_positions = []
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    closed_count = 0
    closed_wins = 0
    for row in positions:
        state = str(row["state"] or "")
        avg_entry_price = float(row["avg_entry_price"] or row["entry_price"] or 0.0)
        shares = float(row["filled_entry_shares"] or row["entry_shares"] or 0.0)
        if state == "closed":
            realized_pnl += float(row["realized_pnl"] or 0.0)
            closed_count += 1
            if float(row["realized_pnl"] or 0.0) > 0:
                closed_wins += 1
            continue

        wallet_val = str(row["wallet"] or "").lower()
        condition_id = str(row["condition_id"] or "").lower()
        outcome = str(row["outcome"] or "").lower()
        current_price = avg_entry_price
        signal_row = conn.execute(
            """
            SELECT current_price, is_open, title, analyzed_ts
            FROM position_signals
            WHERE wallet = ?
              AND condition_id = ?
              AND outcome = ?
            ORDER BY analyzed_ts DESC
            LIMIT 1
            """,
            (wallet_val, condition_id, outcome),
        ).fetchone()
        title = ""
        signal_open = False
        if signal_row is not None:
            current_price = float(signal_row["current_price"] or avg_entry_price)
            title = str(signal_row["title"] or "")
            signal_open = bool(int(signal_row["is_open"] or 0))
        else:
            followed_row = conn.execute(
                """
                SELECT current_price, title
                FROM followed_positions
                WHERE wallet = ?
                  AND condition_id = ?
                  AND outcome = ?
                ORDER BY last_synced_ts DESC
                LIMIT 1
                """,
                (wallet_val, condition_id, outcome),
            ).fetchone()
            if followed_row is not None:
                current_price = float(followed_row["current_price"] or avg_entry_price)
                title = str(followed_row["title"] or "")

        current_value = shares * current_price
        pnl = shares * (current_price - avg_entry_price)
        unrealized_pnl += pnl
        open_positions.append(
            {
                "position_id": int(row["position_id"] or 0),
                "wallet": wallet_val,
                "condition_id": condition_id,
                "outcome": str(row["outcome"] or "").upper(),
                "title": title,
                "state": state,
                "entry_price": avg_entry_price,
                "current_price": current_price,
                "shares": shares,
                "current_value": current_value,
                "unrealized_pnl": pnl,
                "opened_ts": int(row["opened_ts"] or 0),
                "signal_open": signal_open,
            }
        )

    win_rate = round(closed_wins / closed_count * 100.0, 1) if closed_count > 0 else None
    return {
        "open_positions": open_positions,
        "open_positions_count": len(open_positions),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "closed_count": closed_count,
        "win_rate": win_rate,
        "last_run_status": str(last_run["status"] or "") if last_run else "",
        "last_run_started_at": str(last_run["started_at"] or "") if last_run else "",
        "last_run_finished_at": str(last_run["finished_at"] or "") if last_run else "",
        "last_error": str(last_run["error_msg"] or "") if last_run else "",
        "last_run_details": _json_object(last_run["details_json"]) if last_run and last_run["details_json"] else {},
    }


def _strategy_runtime_from_paper_trades(conn, source_wallet: str) -> Dict[str, Any]:
    """Compute strategy runtime metrics from paper_trades for insider_copy strategies.

    Uses the paper-trader ledger as source of truth instead of the experimental
    strategy_positions/strategy_runs tables.
    """
    ensure_paper_trades_duplicate_of_column(conn)
    trades = conn.execute(
        """SELECT * FROM paper_trades WHERE wallet = ? ORDER BY opened_at DESC""",
        (source_wallet,),
    ).fetchall()

    open_positions = []
    realized_pnl = 0.0
    unrealized_pnl = 0.0
    closed_count = 0
    closed_wins = 0

    for t in trades:
        status = str(t["status"] or "")
        entry_price = float(t["entry_price"] or 0.0)
        size_usdc = float(t["size_usdc"] or 0.0)
        shares = float(t["shares"] or 0.0) if t["shares"] else (size_usdc / entry_price if entry_price > 0 else 0.0)

        if status == "open":
            current_price = float(t["current_price"] or entry_price)
            pnl = shares * (current_price - entry_price)
            unrealized_pnl += pnl
            open_positions.append({
                "position_id": int(t["trade_id"] or 0),
                "wallet": str(t["wallet"] or ""),
                "condition_id": str(t["condition_id"] or ""),
                "outcome": str(t["outcome"] or "").upper(),
                "title": str(t["market_title"] or ""),
                "state": "open",
                "entry_price": entry_price,
                "current_price": current_price,
                "shares": shares,
                "current_value": shares * current_price,
                "unrealized_pnl": pnl,
                "opened_ts": str(t["opened_at"] or ""),
                "signal_open": True,
            })
        else:
            # Skip deduped entries — they never executed a real position
            if t["duplicate_of"] is not None:
                continue
            pnl_val = float(t["pnl_usdc"] or 0.0)
            realized_pnl += pnl_val
            closed_count += 1
            if pnl_val > 0:
                closed_wins += 1

    # Use paper trader's last cycle time as "last run"
    last_open = trades[0] if trades else None
    last_run_ts = str(last_open["opened_at"] or "") if last_open else ""

    win_rate = round(closed_wins / closed_count * 100.0, 1) if closed_count > 0 else None
    return {
        "open_positions": open_positions,
        "open_positions_count": len(open_positions),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "closed_count": closed_count,
        "win_rate": win_rate,
        "last_run_status": "ok",
        "last_run_started_at": last_run_ts,
        "last_run_finished_at": "",
        "last_error": "",
        "last_run_details": {},
        "source": "paper_trades",
    }


def _serialize_strategy(
    conn,
    row,
    *,
    label_by_wallet: Dict[str, str] | None = None,
    profile_by_wallet: Dict[str, Dict[str, str]] | None = None,
    include_executions: bool = False,
    include_positions: bool = False,
    include_runs: bool = False,
) -> Dict[str, Any]:
    label_by_wallet = label_by_wallet or {}
    profile_by_wallet = profile_by_wallet or {}
    wallet_key = str(row["wallet"] or "")
    strategy_type = str(row["strategy_type"] or "")
    prof = profile_by_wallet.get(wallet_key.lower(), {})
    runtime = _strategy_runtime(
        conn, int(row["id"] or 0),
        strategy_type=strategy_type,
        wallet=wallet_key,
    )
    item = {
        "id": int(row["id"] or 0),
        "wallet": wallet_key,
        "wallet_label": label_by_wallet.get(wallet_key, ""),
        "display_name": prof.get("display_name", "") or label_by_wallet.get(wallet_key, "") or str(row["label"] or ""),
        "profile_image": prof.get("profile_image", ""),
        "label": str(row["label"] or ""),
        "strategy_type": str(row["strategy_type"] or ""),
        "parameters": _json_object(row["parameters"]),
        "status": str(row["status"] or ""),
        "ai_generated": bool(int(row["ai_generated"] or 0)),
        "ai_analysis": str(row["ai_analysis"] or ""),
        "trades_triggered": int(row["trades_triggered"] or 0),
        "pnl_usdc": float(row["pnl_usdc"] or 0.0),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "open_positions_count": int(runtime["open_positions_count"]),
        "realized_pnl": float(runtime["realized_pnl"]),
        "unrealized_pnl": float(runtime["unrealized_pnl"]),
        "open_unrealized_pnl": float(runtime["unrealized_pnl"]),
        "total_pnl": float(runtime["realized_pnl"]) + float(runtime["unrealized_pnl"]),
        "closed_count": int(runtime.get("closed_count", 0)),
        "win_rate": runtime.get("win_rate"),
        "last_run_status": runtime["last_run_status"],
        "last_run_started_at": runtime["last_run_started_at"],
        "last_run_finished_at": runtime["last_run_finished_at"],
        "last_error": runtime["last_error"],
        "last_run_details": runtime["last_run_details"],
    }

    if include_positions:
        item["positions"] = runtime["open_positions"]

    if include_executions:
        if strategy_type == "insider_copy_v1" and wallet_key:
            # Use paper_trades as execution history for insider_copy strategies
            pt_rows = conn.execute(
                """SELECT * FROM paper_trades
                   WHERE wallet = ?
                   ORDER BY COALESCE(closed_at, opened_at) DESC
                   LIMIT 20""",
                (wallet_key,),
            ).fetchall()
            item["executions"] = [
                {
                    "id": int(e["trade_id"] or 0),
                    "strategy_id": int(row["id"] or 0),
                    "wallet": str(e["wallet"] or ""),
                    "condition_id": str(e["condition_id"] or ""),
                    "outcome": str(e["outcome"] or ""),
                    "title": str(e["market_title"] or ""),
                    "execution_type": "paper",
                    "size_usdc": float(e["size_usdc"] or 0.0),
                    "entry_price": float(e["entry_price"] or 0.0),
                    "token_id": "",
                    "order_id": "",
                    "status": str(e["status"] or ""),
                    "pnl_usdc": float(e["pnl_usdc"] or 0.0),
                    "error_msg": str(e["exit_reason"] or ""),
                    "created_at": str(e["opened_at"] or ""),
                    "updated_at": str(e["closed_at"] or e["opened_at"] or ""),
                }
                for e in pt_rows
            ]
        else:
            executions = conn.execute(
                "SELECT * FROM strategy_executions WHERE strategy_id = ? ORDER BY created_at DESC LIMIT 20",
                (int(row["id"] or 0),),
            ).fetchall()
            item["executions"] = [
                {
                    "id": int(e["id"] or 0),
                    "strategy_id": int(e["strategy_id"] or 0),
                    "wallet": str(e["wallet"] or ""),
                    "condition_id": str(e["condition_id"] or ""),
                    "outcome": str(e["outcome"] or ""),
                    "title": str(e["title"] or ""),
                    "execution_type": str(e["execution_type"] or ""),
                    "size_usdc": float(e["size_usdc"] or 0.0),
                    "entry_price": float(e["entry_price"] or 0.0),
                    "token_id": str(e["token_id"] or ""),
                    "order_id": str(e["order_id"] or ""),
                    "status": str(e["status"] or ""),
                    "pnl_usdc": float(e["pnl_usdc"] or 0.0),
                    "error_msg": str(e["error_msg"] or ""),
                    "created_at": str(e["created_at"] or ""),
                    "updated_at": str(e["updated_at"] or ""),
                }
                for e in executions
            ]

    if include_runs:
        if strategy_type == "insider_copy_v1" and wallet_key:
            # For insider_copy, show paper trade activity summary instead of strategy_runs
            item["runs"] = []
        else:
            runs = conn.execute(
                "SELECT * FROM strategy_runs WHERE strategy_id = ? ORDER BY run_id DESC LIMIT 10",
                (int(row["id"] or 0),),
            ).fetchall()
            item["runs"] = [
                {
                    "run_id": int(r["run_id"] or 0),
                    "status": str(r["status"] or ""),
                    "started_at": str(r["started_at"] or ""),
                    "finished_at": str(r["finished_at"] or ""),
                    "discovered_count": int(r["discovered_count"] or 0),
                    "entered_count": int(r["entered_count"] or 0),
                    "exited_count": int(r["exited_count"] or 0),
                    "open_positions": int(r["open_positions"] or 0),
                    "realized_pnl": float(r["realized_pnl"] or 0.0),
                    "unrealized_pnl": float(r["unrealized_pnl"] or 0.0),
                    "error_msg": str(r["error_msg"] or ""),
                    "details": _json_object(r["details_json"]),
                }
                for r in runs
            ]

    return item


def _paper_trader_card(conn) -> Dict[str, Any]:
    """Build a synthetic strategy card for the paper_trader (all paper_trades rows).

    This covers trades not attributed to any specific strategy_id — i.e. the
    global paper-trader ledger driven by the insider-copy signal pipeline.
    """
    ensure_paper_trades_duplicate_of_column(conn)
    try:
        rows = conn.execute(
            "SELECT status, pnl_usdc, entry_price, current_price, shares, size_usdc, duplicate_of FROM paper_trades"
        ).fetchall()
    except Exception:
        rows = []

    open_rows = [r for r in rows if str(r["status"] or "") == "open"]
    # Exclude deduped entries (duplicate_of IS NOT NULL) from closed stats — they never
    # executed a real position and should not count as wins or losses.
    closed_rows = [r for r in rows if str(r["status"] or "") != "open" and r["duplicate_of"] is None]
    wins = [r for r in closed_rows if float(r["pnl_usdc"] or 0.0) > 0]
    realized_pnl = sum(float(r["pnl_usdc"] or 0.0) for r in closed_rows)

    unrealized_pnl = 0.0
    for r in open_rows:
        entry = float(r["entry_price"] or 0.0)
        current = float(r["current_price"] or entry)
        shares = float(r["shares"] or 0.0)
        if not shares and entry > 0:
            shares = float(r["size_usdc"] or 0.0) / entry
        unrealized_pnl += shares * (current - entry)

    closed_count = len(closed_rows)
    win_rate = round(len(wins) / closed_count * 100.0, 1) if closed_count > 0 else None
    total_pnl = realized_pnl + unrealized_pnl

    return {
        "id": "paper_trader",
        "wallet": "",
        "wallet_label": "",
        "display_name": "Paper Trader (Insider Copy)",
        "profile_image": "",
        "label": "Paper Trader (Insider Copy)",
        "strategy_type": "paper_trader",
        "parameters": {},
        "status": "active",
        "ai_generated": False,
        "ai_analysis": "",
        "trades_triggered": len(rows),
        "pnl_usdc": total_pnl,
        "created_at": "",
        "updated_at": "",
        "open_positions_count": len(open_rows),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "open_unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "closed_count": closed_count,
        "win_rate": win_rate,
        "last_run_status": "active",
        "last_run_started_at": "",
        "last_run_finished_at": "",
        "last_error": "",
        "last_run_details": {},
        "is_paper_trader_card": True,
    }


def create_api_strategies_blueprint(ctx: ApiStrategiesContext) -> Blueprint:
    bp = Blueprint("api_strategies", __name__)

    @bp.get("/api/strategies")
    def api_strategies_list():
        """List all strategies with optional wallet and status filters."""
        try:
            wallet = normalize_wallet(request.args.get("wallet"))
            status_filter = str(request.args.get("status") or "").strip().lower()

            if wallet:
                try:
                    wallet = validate_wallet(wallet)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400

            # Build query
            query = "SELECT * FROM strategies WHERE 1=1"
            params = []

            if wallet:
                query += " AND wallet = ?"
                params.append(wallet)

            if status_filter:
                query += " AND status = ?"
                params.append(status_filter)

            query += " ORDER BY created_at DESC"

            rows = ctx["conn"].execute(query, params).fetchall()

            # Get wallet labels and profile info
            wallets = list(set(str(row["wallet"] or "") for row in rows if row["wallet"]))
            label_by_wallet = {}
            profile_by_wallet: Dict[str, Dict[str, str]] = {}
            if wallets:
                placeholders = ",".join("?" for _ in wallets)
                for label_row in ctx["conn"].execute(
                    f"SELECT wallet, label FROM followed_wallets WHERE wallet IN ({placeholders})",
                    wallets,
                ).fetchall():
                    label_by_wallet[str(label_row["wallet"] or "")] = str(label_row["label"] or "")
                for prof_row in ctx["conn"].execute(
                    f"SELECT wallet, username, pseudonym, profile_image, profile_image_optimized FROM wallet_profiles WHERE LOWER(wallet) IN ({placeholders})",
                    [w.lower() for w in wallets],
                ).fetchall():
                    pw = str(prof_row["wallet"] or "").lower()
                    display = str(prof_row["username"] or "") or str(prof_row["pseudonym"] or "") or ""
                    img = str(prof_row["profile_image_optimized"] or "") or str(prof_row["profile_image"] or "") or ""
                    profile_by_wallet[pw] = {"display_name": display, "profile_image": img}

            data: List[Dict[str, Any]] = []
            for row in rows:
                data.append(
                    _serialize_strategy(
                        ctx["conn"],
                        row,
                        label_by_wallet=label_by_wallet,
                        profile_by_wallet=profile_by_wallet,
                    )
                )

            # Prepend synthetic paper_trader card unless filtering to a specific
            # wallet or a non-active status (paper trader is always active).
            if not wallet and status_filter in ("", "all", "active"):
                try:
                    pt_card = _paper_trader_card(ctx["conn"])
                    data.insert(0, pt_card)
                except Exception:
                    logger.warning("Failed to build paper_trader_card", exc_info=True)

            return jsonify({"count": len(data), "items": data})

        except Exception as e:
            logger.exception("Error listing strategies")
            return jsonify({"error": "Internal server error"}), 500

    @bp.post("/api/strategies")
    def api_strategies_create():
        """Create a new strategy."""
        try:
            payload = request.get_json(silent=True) or {}
            label = str(payload.get("label") or "").strip()
            status = str(payload.get("status") or "active").strip().lower()

            try:
                wallet = validate_wallet(payload.get("wallet"))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

            try:
                strategy_type = validate_strategy_type(payload.get("strategy_type"))
            except ValueError as e:
                return jsonify({"error": str(e)}), 400

            if status not in {"active", "paused", "stopped"}:
                return jsonify({"error": "status must be one of: active, paused, stopped"}), 400

            parameters = validate_parameters(payload.get("parameters", {}))

            from db import insert_strategy

            strategy_id = insert_strategy(
                ctx["conn"],
                wallet=wallet,
                label=label,
                strategy_type=strategy_type,
                parameters=parameters,
                status=status,
            )

            row = ctx["conn"].execute(
                "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()

            return jsonify(_serialize_strategy(ctx["conn"], row)), 201

        except Exception as e:
            logger.exception("Error creating strategy")
            return jsonify({"error": "Internal server error"}), 500

    @bp.get("/api/strategies/<int:strategy_id>")
    def api_strategies_get(strategy_id: int):
        """Get a single strategy with its last 20 executions."""
        try:
            row = ctx["conn"].execute(
                "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()

            if not row:
                return jsonify({"error": "strategy not found"}), 404

            wallet_key = str(row["wallet"] or "")
            label_by_wallet: Dict[str, str] = {}
            profile_by_wallet: Dict[str, Dict[str, str]] = {}
            if wallet_key:
                label_row = ctx["conn"].execute(
                    "SELECT wallet, label FROM followed_wallets WHERE wallet = ?",
                    (wallet_key,),
                ).fetchone()
                if label_row is not None:
                    label_by_wallet[wallet_key] = str(label_row["label"] or "")
                prof_row = ctx["conn"].execute(
                    """
                    SELECT wallet, username, pseudonym, profile_image, profile_image_optimized
                    FROM wallet_profiles
                    WHERE LOWER(wallet) = ?
                    """,
                    (wallet_key.lower(),),
                ).fetchone()
                if prof_row is not None:
                    profile_by_wallet[wallet_key.lower()] = {
                        "display_name": str(prof_row["username"] or "") or str(prof_row["pseudonym"] or "") or "",
                        "profile_image": str(prof_row["profile_image_optimized"] or "") or str(prof_row["profile_image"] or "") or "",
                    }

            return jsonify(
                _serialize_strategy(
                    ctx["conn"],
                    row,
                    label_by_wallet=label_by_wallet,
                    profile_by_wallet=profile_by_wallet,
                    include_executions=True,
                    include_positions=True,
                    include_runs=True,
                )
            )

        except Exception as e:
            logger.exception("Error getting strategy")
            return jsonify({"error": "Internal server error"}), 500

    @bp.get("/api/strategies/paper_trader")
    def api_strategies_get_paper_trader():
        """Detail for the synthetic paper_trader card. The list route prepends this card with a
        STRING id 'paper_trader', so the int-only detail route 404s on a direct GET; serve the
        same synthetic card here so the URL resolves. (review #LOW 2026-06-17)"""
        try:
            return jsonify(_paper_trader_card(ctx["conn"])), 200
        except Exception:
            logger.warning("Failed to build paper_trader_card", exc_info=True)
            return jsonify({"error": "paper_trader card unavailable"}), 500

    @bp.patch("/api/strategies/<int:strategy_id>")
    def api_strategies_update(strategy_id: int):
        """Update a strategy (status, parameters, label)."""
        try:
            payload = request.get_json(silent=True) or {}

            row = ctx["conn"].execute(
                "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()

            if not row:
                return jsonify({"error": "strategy not found"}), 404

            # Build update
            updates = {}
            if "status" in payload:
                status = str(payload.get("status") or "").strip().lower()
                if status not in {"active", "paused", "stopped"}:
                    return jsonify({"error": "status must be one of: active, paused, stopped"}), 400
                updates["status"] = status

            if "strategy_type" in payload:
                try:
                    updates["strategy_type"] = validate_strategy_type(payload["strategy_type"])
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400

            if "parameters" in payload:
                updates["parameters"] = json.dumps(validate_parameters(payload["parameters"]))

            if "label" in payload:
                updates["label"] = str(payload.get("label") or "").strip()

            if "lifecycle_stage" in payload:
                stage = str(payload.get("lifecycle_stage") or "").strip().lower()
                if stage not in {"research", "paper", "promoted", "retired"}:
                    return jsonify({"error": "lifecycle_stage must be one of: research, paper, promoted, retired"}), 400
                updates["lifecycle_stage"] = stage

            if not updates:
                return jsonify({"error": "no valid fields to update"}), 400

            updates["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # Build dynamic UPDATE query (whitelist columns to prevent injection)
            ALLOWED_UPDATE_COLS = frozenset({
                "name", "description", "strategy_type", "wallet", "parameters",
                "status", "label", "lifecycle_stage",
                "is_active", "is_paper", "execution_mode", "updated_at",
            })
            updates = {k: v for k, v in updates.items() if k in ALLOWED_UPDATE_COLS}
            if not updates:
                return jsonify({"error": "no allowed fields to update"}), 400
            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            query = f"UPDATE strategies SET {set_clause} WHERE id = ?"
            values = list(updates.values()) + [strategy_id]

            ctx["conn"].execute(query, values)
            ctx["conn"].commit()

            # Return updated strategy
            row = ctx["conn"].execute(
                "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()

            return jsonify(_serialize_strategy(ctx["conn"], row))

        except Exception as e:
            logger.exception("Error updating strategy")
            return jsonify({"error": "Internal server error"}), 500

    @bp.delete("/api/strategies/<int:strategy_id>")
    def api_strategies_delete(strategy_id: int):
        """Delete a strategy."""
        try:
            row = ctx["conn"].execute(
                "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()

            if not row:
                return jsonify({"error": "strategy not found"}), 404

            ctx["conn"].execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
            ctx["conn"].commit()

            return jsonify({"ok": True})

        except Exception as e:
            logger.exception("Error deleting strategy")
            return jsonify({"error": "Internal server error"}), 500

    @bp.get("/api/strategies/<int:strategy_id>/executions")
    def api_strategies_executions(strategy_id: int):
        """List executions for a strategy."""
        try:
            # Verify strategy exists
            row = ctx["conn"].execute(
                "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()

            if not row:
                return jsonify({"error": "strategy not found"}), 404

            limit = ctx["safe_int"](request.args.get("limit"), 50)
            if limit <= 0:
                limit = 50
            limit = min(limit, 500)

            strategy_type = str(row["strategy_type"] or "")
            wallet_key = str(row["wallet"] or "")

            if strategy_type == "insider_copy_v1" and wallet_key:
                pt_rows = ctx["conn"].execute(
                    """SELECT * FROM paper_trades
                       WHERE wallet = ?
                       ORDER BY COALESCE(closed_at, opened_at) DESC
                       LIMIT ?""",
                    (wallet_key, limit),
                ).fetchall()
                data = [
                    {
                        "id": int(e["trade_id"] or 0),
                        "strategy_id": strategy_id,
                        "wallet": str(e["wallet"] or ""),
                        "condition_id": str(e["condition_id"] or ""),
                        "outcome": str(e["outcome"] or ""),
                        "title": str(e["market_title"] or ""),
                        "execution_type": "paper",
                        "size_usdc": float(e["size_usdc"] or 0.0),
                        "entry_price": float(e["entry_price"] or 0.0),
                        "token_id": "",
                        "order_id": "",
                        "status": str(e["status"] or ""),
                        "pnl_usdc": float(e["pnl_usdc"] or 0.0),
                        "error_msg": str(e["exit_reason"] or ""),
                        "created_at": str(e["opened_at"] or ""),
                        "updated_at": str(e["closed_at"] or e["opened_at"] or ""),
                    }
                    for e in pt_rows
                ]
            else:
                executions = ctx["conn"].execute(
                    "SELECT * FROM strategy_executions WHERE strategy_id = ? ORDER BY created_at DESC LIMIT ?",
                    (strategy_id, limit),
                ).fetchall()
                data = [
                    {
                        "id": int(e["id"] or 0),
                        "strategy_id": int(e["strategy_id"] or 0),
                        "wallet": str(e["wallet"] or ""),
                        "condition_id": str(e["condition_id"] or ""),
                        "outcome": str(e["outcome"] or ""),
                        "title": str(e["title"] or ""),
                        "execution_type": str(e["execution_type"] or ""),
                        "size_usdc": float(e["size_usdc"] or 0.0),
                        "entry_price": float(e["entry_price"] or 0.0),
                        "token_id": str(e["token_id"] or ""),
                        "order_id": str(e["order_id"] or ""),
                        "status": str(e["status"] or ""),
                        "pnl_usdc": float(e["pnl_usdc"] or 0.0),
                        "error_msg": str(e["error_msg"] or ""),
                        "created_at": str(e["created_at"] or ""),
                        "updated_at": str(e["updated_at"] or ""),
                    }
                    for e in executions
                ]

            return jsonify({"count": len(data), "items": data})

        except Exception as e:
            logger.exception("Error getting strategy executions")
            return jsonify({"error": "Internal server error"}), 500

    @bp.post("/api/strategies/analyze-wallet")
    def api_strategies_analyze_wallet():
        """AI analysis of wallet trading patterns."""
        try:
            payload = request.get_json(silent=True) or {}
            wallet = ctx["normalize_wallet"](payload.get("wallet"))

            if not ctx["wallet_valid"](wallet):
                return jsonify({"error": "wallet must be a valid 0x address"}), 400

            # Query watchlist_trades for last 90 days
            ninety_days_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
            ninety_days_ago_str = ninety_days_ago.strftime("%Y-%m-%d %H:%M:%S")

            trades = ctx["conn"].execute(
                """
                SELECT * FROM watchlist_trades
                WHERE wallet = ? AND timestamp >= ?
                ORDER BY timestamp DESC
                """,
                (wallet, ninety_days_ago_str),
            ).fetchall()

            # Compute stats
            stats = {
                "total_trades": len(trades),
                "win_rate": 0.0,
                "avg_entry_price": 0.0,
                "avg_notional": 0.0,
                "favorite_categories": [],
                "avg_hold_time_hours": None,
                "pnl_estimate": 0.0,
            }

            if trades:
                # Win rate: count trades with entry price < 0.80
                winning_trades = [
                    t
                    for t in trades
                    if float(t["price"] or 0.0) < 0.80
                ]
                stats["win_rate"] = (
                    len(winning_trades) / len(trades) * 100
                    if trades
                    else 0.0
                )

                # Average entry price
                prices = [float(t["price"] or 0.0) for t in trades]
                stats["avg_entry_price"] = (
                    sum(prices) / len(prices) if prices else 0.0
                )

                # Average notional
                notionals = [float(t["notional"] or 0.0) for t in trades]
                stats["avg_notional"] = (
                    sum(notionals) / len(notionals) if notionals else 0.0
                )

                # Favorite categories from title keywords
                category_counts = {
                    "politics": 0,
                    "sports": 0,
                    "crypto": 0,
                    "geopolitics": 0,
                    "other": 0,
                }
                for trade in trades:
                    title = str(trade["title"] or "").lower()
                    if any(
                        word in title
                        for word in ["trump", "harris", "biden", "senate", "congress", "vote", "election"]
                    ):
                        category_counts["politics"] += 1
                    elif any(
                        word in title
                        for word in ["nfl", "nba", "mlb", "nhl", "super bowl", "world series", "stanley cup"]
                    ):
                        category_counts["sports"] += 1
                    elif any(
                        word in title
                        for word in ["bitcoin", "ethereum", "crypto", "xrp", "solana", "dogecoin"]
                    ):
                        category_counts["crypto"] += 1
                    elif any(
                        word in title
                        for word in ["russia", "ukraine", "china", "israel", "iran", "north korea", "middle east"]
                    ):
                        category_counts["geopolitics"] += 1
                    else:
                        category_counts["other"] += 1

                # Sort by count
                sorted_categories = sorted(
                    category_counts.items(), key=lambda x: x[1], reverse=True
                )
                stats["favorite_categories"] = [
                    cat for cat, count in sorted_categories if count > 0
                ]

                # Average hold time (if timestamps available)
                if len(trades) > 1:
                    timestamps = []
                    for t in trades:
                        try:
                            ts = dt.datetime.strptime(
                                str(t["timestamp"] or ""), "%Y-%m-%d %H:%M:%S"
                            )
                            timestamps.append(ts)
                        except (ValueError, AttributeError):
                            pass

                    if len(timestamps) > 1:
                        timestamps.sort()
                        time_diffs = []
                        for i in range(len(timestamps) - 1):
                            diff = (timestamps[i + 1] - timestamps[i]).total_seconds() / 3600
                            if diff > 0:
                                time_diffs.append(diff)

                        if time_diffs:
                            stats["avg_hold_time_hours"] = (
                                sum(time_diffs) / len(time_diffs)
                            )

                # PnL estimate (naive: avg price movement)
                stats["pnl_estimate"] = (
                    (0.5 - stats["avg_entry_price"])
                    * stats["avg_notional"]
                    * stats["total_trades"]
                )

            # Call Ollama for AI analysis
            recommendation = None
            raw_ai_output = None
            error_msg = None

            try:
                # First, check available models
                models_resp = requests.get(
                    "http://localhost:11434/api/tags",
                    timeout=5,
                )
                models_resp.raise_for_status()
                available_models = models_resp.json().get("models", [])
                model_name = "qwen2.5-coder"

                # Check if model exists, otherwise use first available
                if not any(m.get("name", "").startswith(model_name) for m in available_models):
                    if available_models:
                        model_name = available_models[0].get("name", "qwen2.5-coder")

                # Build prompt
                prompt = f"""Analyze this Polymarket trader's statistics and recommend a trading strategy:

Total trades (90 days): {stats['total_trades']}
Win rate (entry < 0.80): {stats['win_rate']:.1f}%
Average entry price: ${stats['avg_entry_price']:.2f}
Average notional: ${stats['avg_notional']:.2f}
Favorite categories: {', '.join(stats['favorite_categories']) if stats['favorite_categories'] else 'diverse'}
Average hold time: {f"{stats['avg_hold_time_hours']:.1f} hours" if stats['avg_hold_time_hours'] else 'N/A'}
Estimated PnL: ${stats['pnl_estimate']:.2f}

Recommend a strategy type (mirror_all, selective, signal_only, fade_v1, or insider_copy_v1) and provide specific parameters as JSON.
Include 2-3 sentences of reasoning."""

                # Call Ollama
                ollama_resp = requests.post(
                    "http://localhost:11434/api/generate",
                    json={
                        "model": model_name,
                        "prompt": prompt,
                        "stream": False,
                    },
                    timeout=30,
                )
                ollama_resp.raise_for_status()
                raw_ai_output = ollama_resp.json().get("response", "")

                # Parse recommendation from response. Prefer longer IDs first so
                # fade_v1 is not accidentally matched as legacy fade.
                response_lower = raw_ai_output.lower()
                strategy_type = None
                for st in sorted(VALID_STRATEGY_TYPES, key=len, reverse=True):
                    if st in response_lower:
                        strategy_type = st
                        break

                if strategy_type == "fade":
                    strategy_type = "fade_v1"

                if strategy_type:
                    # Try to extract JSON parameters from response
                    parameters = {}
                    try:
                        # Look for JSON block in response
                        import re

                        json_match = re.search(
                            r"\{[^{}]*\}", raw_ai_output
                        )
                        if json_match:
                            parameters = json.loads(json_match.group())
                    except (ValueError, KeyError):
                        parameters = {
                            "recommended_by": "ai_analysis",
                            "wallet_type": "suspected_" + ("pro" if stats["win_rate"] > 55 else "amateur"),
                        }

                    recommendation = {
                        "strategy_type": strategy_type,
                        "parameters": parameters,
                        "reasoning": raw_ai_output[:500],  # First 500 chars
                    }

            except requests.exceptions.Timeout:
                error_msg = "AI offline"
            except requests.exceptions.ConnectionError:
                error_msg = "AI offline"
            except Exception as e:
                logger.exception("Error calling Ollama")
                error_msg = "AI offline"

            result = {
                "wallet": wallet,
                "stats": stats,
                "recommendation": recommendation,
            }

            if error_msg:
                result["error"] = error_msg
            else:
                result["raw_ai_output"] = raw_ai_output

            return jsonify(result)

        except Exception as e:
            logger.exception("Error analyzing wallet")
            return jsonify({"error": "Internal server error"}), 500

    @bp.get("/api/copy-pipeline/wallets")
    def api_copy_pipeline_wallets_list():
        """List wallets explicitly enrolled in the copy pipeline."""
        try:
            status = str(request.args.get("status") or "active").strip().lower() or "active"
            items = []
            for row in get_followed_wallets(status=status):
                items.append(
                    {
                        "wallet": str(row["wallet"] or ""),
                        "label": str(row["label"] or ""),
                        "status": str(row["status"] or status),
                        "reason": str(row["reason"] or ""),
                        "added_at": str(row["added_at"] or ""),
                        "strategy_json": str(row["strategy_json"] or ""),
                        "total_positions": int(row["total_positions"] or 0),
                        "total_pnl_usdc": float(row["total_pnl_usdc"] or 0.0),
                        "last_copied_at": str(row["last_copied_at"] or ""),
                    }
                )
            return jsonify({"items": items, "count": len(items), "status": status})
        except Exception:
            logger.exception("Error listing copy-pipeline wallets")
            return jsonify({"error": "Internal server error"}), 500

    @bp.post("/api/copy-pipeline/wallets")
    def api_copy_pipeline_wallets_add():
        """Add a wallet to the active copy pipeline."""
        try:
            payload = request.get_json(silent=True) or {}
            wallet = ctx["normalize_wallet"](payload.get("wallet"))
            label = str(payload.get("label") or "").strip()
            reason = str(payload.get("reason") or "").strip()

            if not ctx["wallet_valid"](wallet):
                return jsonify({"error": "wallet must be a valid 0x address"}), 400

            upsert_followed_wallet_record(
                ctx["conn"],
                wallet=wallet,
                label=label,
                reason=reason,
                status="active",
                total_positions=0,
                total_pnl_usdc=0.0,
            )
            ctx["conn"].commit()

            return jsonify({"ok": True, "wallet": wallet})

        except Exception:
            logger.exception("Error adding copy-pipeline wallet")
            return jsonify({"error": "Internal server error"}), 500

    @bp.delete("/api/copy-pipeline/wallets/<wallet>")
    def api_copy_pipeline_wallets_remove(wallet: str):
        """Remove a wallet from the active copy pipeline."""
        try:
            wallet = ctx["normalize_wallet"](wallet)

            if not ctx["wallet_valid"](wallet):
                return jsonify({"error": "wallet must be a valid 0x address"}), 400

            set_followed_wallet_status(
                ctx["conn"],
                wallet=wallet,
                status="unfollowed",
                updated_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )
            ctx["conn"].commit()

            return jsonify({"ok": True})

        except Exception:
            logger.exception("Error removing copy-pipeline wallet")
            return jsonify({"error": "Internal server error"}), 500

    # ------------------------------------------------------------------
    # Copy viability scoring
    # ------------------------------------------------------------------

    @bp.get("/api/copy-viability")
    def api_copy_viability():
        """Get copy viability score for a wallet."""
        try:
            from sniper.analysis.copy_viability import measure_wallet_viability
            wallet = request.args.get("wallet", "").strip()
            if not wallet:
                return jsonify({"error": "wallet parameter required"}), 400
            result = measure_wallet_viability(ctx["conn"], wallet)
            return jsonify(result)
        except Exception as exc:
            logger.exception("api_copy_viability failed")
            return jsonify({"error": "Internal server error"}), 500

    @bp.get("/api/copy-viability/leaderboard")
    def api_copy_viability_leaderboard():
        """Top wallets by copy viability score."""
        try:
            from sniper.analysis.copy_viability import update_all_viability, get_viability_leaderboard
            # TODO: move update_all_viability to a periodic batch job for scale.
            # Inline here is acceptable at current volume but will slow down with
            # many wallets in signal_queue.
            update_all_viability(ctx["conn"])
            limit = min(int(request.args.get("limit", "20")), 100)
            results = get_viability_leaderboard(ctx["conn"], limit=limit)
            return jsonify({"items": results, "count": len(results)})
        except Exception as exc:
            logger.exception("api_copy_viability_leaderboard failed")
            return jsonify({"error": "Internal server error"}), 500

    # ------------------------------------------------------------------
    # Retrospective Analyzer endpoints
    # ------------------------------------------------------------------

    @bp.route("/api/analyze/run", methods=["POST"])
    def analyze_run():
        """Run a retrospective signal-outcome analysis.

        POST JSON body (all optional):
            data_source: "paper_trades" | "position_signals" | "fwa"
            min_insider_score: float
            max_speculator_score: float
            max_entry_price: float
            base_size_usdc: float
        """
        from sniper.analysis.retrospective_analyzer import (
            AnalysisConfig,
            run_analysis,
            VALID_DATA_SOURCES,
        )

        body = request.get_json(silent=True) or {}
        data_source = body.get("data_source", "paper_trades")
        if data_source not in VALID_DATA_SOURCES:
            return jsonify({"error": f"Invalid data_source. Valid: {sorted(VALID_DATA_SOURCES)}"}), 400

        skip_raw = body.get("skip_classifications") or []
        config = AnalysisConfig(
            data_source=data_source,
            min_insider_score=body.get("min_insider_score"),
            max_speculator_score=body.get("max_speculator_score"),
            max_entry_price=body.get("max_entry_price"),
            min_qualifying_roi=body.get("min_qualifying_roi"),
            skip_classifications=frozenset(skip_raw) if skip_raw else frozenset(),
            base_size_usdc=float(body.get("base_size_usdc", 25.0)),
        )

        try:
            conn = ctx["conn"]
            result = run_analysis(config, conn=conn)
            return jsonify({
                "data_source": result.data_source,
                "bias_warning": result.bias_warning,
                "pre_filter_count": result.pre_filter_count,
                "trade_count": result.trade_count,
                "win_rate": result.win_rate,
                "total_pnl": result.total_pnl,
                "avg_roi": result.avg_roi,
                "max_drawdown": result.max_drawdown,
                "gate_config": result.gate_config,
                "equity_curve": result.equity_curve,
                "trades": result.trades,
            })
        except Exception:
            logger.exception("analyze_run failed")
            return jsonify({"error": "Analysis failed"}), 500

    @bp.route("/api/analyze/sweep", methods=["POST"])
    def analyze_sweep():
        """Sweep insider_score thresholds and return metrics for each."""
        from sniper.analysis.retrospective_analyzer import (
            AnalysisConfig,
            sweep_insider_threshold,
        )

        body = request.get_json(silent=True) or {}
        data_source = body.get("data_source", "paper_trades")
        thresholds = body.get("thresholds")

        base_config = AnalysisConfig(
            data_source=data_source,
            max_speculator_score=body.get("max_speculator_score"),
            max_entry_price=body.get("max_entry_price"),
            base_size_usdc=float(body.get("base_size_usdc", 25.0)),
        )

        try:
            conn = ctx["conn"]
            results = sweep_insider_threshold(
                data_source=data_source,
                thresholds=thresholds,
                base_config=base_config,
                conn=conn,
            )
            return jsonify({"sweep": results, "data_source": data_source})
        except Exception:
            logger.exception("analyze_sweep failed")
            return jsonify({"error": "Sweep failed"}), 500

    @bp.route("/api/analyze/sweep2d", methods=["POST"])
    def analyze_sweep_2d():
        """2D parameter sweep: insider_score x max_entry_price heatmap."""
        from sniper.analysis.retrospective_analyzer import AnalysisConfig, sweep_2d

        body = request.get_json(silent=True) or {}
        data_source = body.get("data_source", "paper_trades")

        base_config = AnalysisConfig(
            data_source=data_source,
            max_speculator_score=body.get("max_speculator_score"),
            base_size_usdc=float(body.get("base_size_usdc", 25.0)),
        )

        try:
            conn = ctx["conn"]
            result = sweep_2d(
                data_source=data_source,
                insider_thresholds=body.get("insider_thresholds"),
                price_thresholds=body.get("price_thresholds"),
                base_config=base_config,
                conn=conn,
            )
            return jsonify(result)
        except Exception:
            logger.exception("analyze_sweep_2d failed")
            return jsonify({"error": "2D sweep failed"}), 500

    # ------------------------------------------------------------------
    # Replay Engine endpoints
    # ------------------------------------------------------------------

    @bp.route("/api/replay/run", methods=["POST"])
    def replay_run():
        """Execute a replay run with the given config."""
        import time as _time
        from sniper.analysis.replay_engine import ReplayConfig, run_replay
        from db import save_replay_config, save_replay_run
        from sniper.settings.web import (
            REPLAY_DEFAULT_BANKROLL, REPLAY_DEFAULT_BASE_SIZE,
            REPLAY_DEFAULT_MIN_INSIDER, REPLAY_DEFAULT_MAX_SPEC,
            REPLAY_DEFAULT_MAX_PRICE, REPLAY_DEFAULT_MAX_CONCURRENT,
            REPLAY_DEFAULT_MAX_MARKET_EXPOSURE, REPLAY_DEFAULT_MAX_TOTAL_EXPOSURE,
        )

        body = request.get_json(silent=True) or {}

        try:
            bankroll = float(body.get("initial_bankroll", REPLAY_DEFAULT_BANKROLL))
            if bankroll <= 0:
                return jsonify({"error": "initial_bankroll must be positive"}), 400

            config = ReplayConfig(
                strategy_type=str(body.get("strategy_type") or "insider_copy_v1").strip().lower(),
                initial_bankroll=bankroll,
                base_size_usdc=float(body.get("base_size_usdc", REPLAY_DEFAULT_BASE_SIZE)),
                max_concurrent_positions=int(body.get("max_concurrent_positions", REPLAY_DEFAULT_MAX_CONCURRENT)),
                max_exposure_per_market_usdc=float(body.get("max_exposure_per_market_usdc", REPLAY_DEFAULT_MAX_MARKET_EXPOSURE)),
                max_total_exposure_usdc=float(body.get("max_total_exposure_usdc", REPLAY_DEFAULT_MAX_TOTAL_EXPOSURE)),
                min_insider_score=float(body.get("min_insider_score", REPLAY_DEFAULT_MIN_INSIDER)),
                max_speculator_score=float(body.get("max_speculator_score", REPLAY_DEFAULT_MAX_SPEC)),
                max_entry_price=float(body.get("max_entry_price", REPLAY_DEFAULT_MAX_PRICE)),
                circuit_breaker_max_consecutive_losses=(
                    int(body["circuit_breaker_max_consecutive_losses"])
                    if body.get("circuit_breaker_max_consecutive_losses") is not None else None
                ),
                circuit_breaker_max_drawdown_pct=(
                    float(body["circuit_breaker_max_drawdown_pct"])
                    if body.get("circuit_breaker_max_drawdown_pct") is not None else None
                ),
                start_ts=int(body["start_ts"]) if body.get("start_ts") is not None else None,
                end_ts=int(body["end_ts"]) if body.get("end_ts") is not None else None,
            )
        except (ValueError, TypeError) as e:
            return jsonify({"error": f"Invalid config: {e}"}), 400

        conn = ctx["conn"]
        start = _time.monotonic()
        try:
            result = run_replay(conn, config)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            logger.exception("replay_run failed")
            return jsonify({"error": "Replay execution failed"}), 500
        duration_ms = int((_time.monotonic() - start) * 1000)

        response = result.to_dict()
        response["duration_ms"] = duration_ms

        save_name = body.get("save_as")
        config_id = None
        if save_name:
            try:
                config_id = save_replay_config(
                    conn, name=save_name, config_json=config.to_dict(),
                    notes=body.get("notes", ""),
                )
            except Exception:
                logger.warning("Could not save replay config %r (may already exist)", save_name)

        run_id = save_replay_run(
            conn, config_id=config_id, config_json=config.to_dict(),
            summary_json=result.summary, result_json=result.to_dict(),
            signal_count=result.summary.get("total_signals", len(result.trades) + len(result.skipped)),
            trade_count=result.summary.get("total_trades", 0),
            duration_ms=duration_ms,
        )
        response["run_id"] = run_id

        return jsonify(response)

    @bp.route("/api/replay/configs", methods=["GET"])
    def replay_list_configs():
        """List all saved replay configurations."""
        from db import list_replay_configs
        try:
            return jsonify(list_replay_configs(ctx["conn"]))
        except Exception:
            logger.exception("replay_list_configs failed")
            return jsonify({"error": "Failed to list configs"}), 500

    @bp.route("/api/replay/runs", methods=["GET"])
    def replay_list_runs():
        """List replay runs, optionally filtered by config_id."""
        from db import list_replay_runs
        config_id = request.args.get("config_id", type=int)
        limit = request.args.get("limit", 50, type=int)
        try:
            return jsonify(list_replay_runs(ctx["conn"], config_id=config_id, limit=limit))
        except Exception:
            logger.exception("replay_list_runs failed")
            return jsonify({"error": "Failed to list runs"}), 500

    @bp.route("/api/replay/runs/<int:run_id>", methods=["GET"])
    def replay_get_run(run_id):
        """Get a single replay run by ID."""
        from db import get_replay_run
        try:
            row = get_replay_run(ctx["conn"], run_id)
            if not row:
                return jsonify({"error": "Run not found"}), 404
            return jsonify(row)
        except Exception:
            logger.exception("replay_get_run failed")
            return jsonify({"error": "Failed to get run"}), 500

    @bp.route("/api/replay/compare", methods=["GET"])
    def replay_compare():
        """Compare two or more replay runs side by side."""
        from db import get_replay_run

        run_ids_str = request.args.get("run_ids", "")
        try:
            run_ids = [int(x.strip()) for x in run_ids_str.split(",") if x.strip()]
        except ValueError:
            return jsonify({"error": "run_ids must be comma-separated integers"}), 400

        if len(run_ids) < 2:
            return jsonify({"error": "Need at least 2 run_ids to compare"}), 400

        conn = ctx["conn"]
        runs = []
        for rid in run_ids:
            row = get_replay_run(conn, rid)
            if not row:
                return jsonify({"error": f"Run {rid} not found"}), 404
            runs.append({
                "run_id": rid,
                "config": json.loads(row["config_json"]) if isinstance(row["config_json"], str) else row["config_json"],
                "summary": json.loads(row["summary_json"]) if isinstance(row["summary_json"], str) else row["summary_json"],
                "duration_ms": row.get("duration_ms"),
                "created_at": row.get("created_at"),
            })

        config_diff = {}
        if len(runs) >= 2:
            all_keys = set()
            for r in runs:
                all_keys.update(r["config"].keys())
            for key in sorted(all_keys):
                values = [r["config"].get(key) for r in runs]
                if len(set(str(v) for v in values)) > 1:
                    config_diff[key] = values

        return jsonify({"runs": runs, "config_diff": config_diff})

    # ------------------------------------------------------------------
    # Sports market analysis endpoints
    # ------------------------------------------------------------------

    @bp.route("/api/sports/edge-observations", methods=["GET"])
    def sports_edge_observations():
        """Return edge observation history for threshold tuning."""
        limit = int(request.args.get("limit", 100))
        sport_key = request.args.get("sport_key", "")
        conn = ctx["conn"]
        try:
            where = "WHERE 1=1"
            params: list = []
            if sport_key:
                where += " AND sport_key = ?"
                params.append(sport_key)
            rows = conn.execute(
                f"SELECT * FROM edge_observations {where} "
                "ORDER BY observed_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            return jsonify({
                "observations": [dict(r) for r in rows],
                "count": len(rows),
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/sports/price-history", methods=["GET"])
    def sports_price_history():
        """Return Polymarket price snapshots for a condition_id."""
        cid = request.args.get("condition_id", "")
        outcome = request.args.get("outcome", "")
        limit = int(request.args.get("limit", 200))
        if not cid:
            return jsonify({"error": "condition_id required"}), 400
        conn = ctx["conn"]
        try:
            from db import poly_price_history
            history = poly_price_history(conn, cid, outcome, limit)
            return jsonify({"history": history, "count": len(history)})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/sports/wallet-edge", methods=["GET"])
    def sports_wallet_edge():
        """Cross-reference wallet sports entries against sportsbook odds."""
        wallet = request.args.get("wallet", "")
        limit = int(request.args.get("limit", 100))
        conn = ctx["conn"]
        try:
            from db import wallet_sports_entry_edge
            results = wallet_sports_entry_edge(conn, wallet, limit=limit)
            # Compute summary stats
            with_edge = [r for r in results if r.get("edge_at_entry") is not None]
            summary = {}
            if with_edge:
                edges = [r["edge_at_entry"] for r in with_edge]
                summary = {
                    "matched_trades": len(with_edge),
                    "unmatched_trades": len(results) - len(with_edge),
                    "avg_edge_at_entry": round(sum(edges) / len(edges), 4),
                    "max_edge_at_entry": round(max(edges), 4),
                    "min_edge_at_entry": round(min(edges), 4),
                    "positive_edge_count": sum(1 for e in edges if e > 0),
                    "negative_edge_count": sum(1 for e in edges if e < 0),
                }
            return jsonify({
                "trades": results,
                "count": len(results),
                "summary": summary,
            })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------
    # HL Liquidation Tracker (monitoring only — Phase 5B adds strategy)
    # ------------------------------------------------------------------

    @bp.get("/api/algorithms/hl-liquidation/dashboard")
    def hl_liquidation_dashboard():
        try:
            positions = ctx["conn"].execute("""
                SELECT wp.*, tw.label
                FROM hl_whale_positions wp
                LEFT JOIN hl_tracked_wallets tw ON wp.wallet = tw.wallet
                ORDER BY wp.distance_pct ASC
                LIMIT 500
            """).fetchall()
            pos_list = [dict(r) for r in positions]
            closest = pos_list[0]["distance_pct"] if pos_list else 100.0
            cross_count = sum(1 for p in pos_list if str(p.get("margin_type") or "").lower() == "cross")
            isolated_count = sum(1 for p in pos_list if str(p.get("margin_type") or "").lower() == "isolated")
            explicit_liq_count = sum(1 for p in pos_list if p.get("liquidation_price") is not None)
            # Monitoring only: clear / approaching / watch. No "imminent" or signal_ready.
            threat_level = "watch" if closest < 5 else "approaching" if closest < 15 else "clear"
            closest_pos = pos_list[0] if pos_list else None
            return jsonify({
                "threat_level": threat_level,
                "closest_distance_pct": round(closest, 2),
                "closest_position": {
                    "wallet": closest_pos["wallet"], "asset": closest_pos["asset"],
                    "side": closest_pos["side"], "leverage": closest_pos["leverage"],
                    "margin_type": closest_pos.get("margin_type"),
                    "liquidation_price": closest_pos.get("liquidation_price"),
                } if closest_pos else None,
                "wallet_count": len(set(p["wallet"] for p in pos_list)),
                "position_count": len(pos_list),
                "long_count": sum(1 for p in pos_list if p["side"] == "long"),
                "short_count": sum(1 for p in pos_list if p["side"] == "short"),
                "danger_count": sum(1 for p in pos_list if p["distance_pct"] < 15),
                "cross_count": cross_count,
                "isolated_count": isolated_count,
                "explicit_liq_count": explicit_liq_count,
                "last_polled_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "positions": pos_list,
            })
        except Exception:
            logger.exception("hl_liquidation_dashboard failed")
            return jsonify({"error": "Internal server error"}), 500

    @bp.get("/api/algorithms/hl-liquidation/wallet/<wallet>")
    def hl_liquidation_wallet(wallet):
        try:
            wallet_lower = wallet.strip().lower()
            positions = ctx["conn"].execute(
                "SELECT * FROM hl_whale_positions WHERE wallet = ? ORDER BY distance_pct ASC",
                (wallet_lower,),
            ).fetchall()
            tracked = ctx["conn"].execute(
                "SELECT * FROM hl_tracked_wallets WHERE wallet = ?", (wallet_lower,),
            ).fetchone()
            return jsonify({
                "wallet": wallet_lower,
                "label": tracked["label"] if tracked else "",
                "source": tracked["source"] if tracked else "",
                "added_at": tracked["added_at"] if tracked else "",
                "position_count": len(positions),
                "positions": [dict(r) for r in positions],
            })
        except Exception:
            logger.exception("hl_liquidation_wallet failed")
            return jsonify({"error": "Internal server error"}), 500

    @bp.get("/api/algorithms/hl-liquidation/state")
    def hl_liquidation_state():
        try:
            wallet_count = ctx["conn"].execute("SELECT COUNT(DISTINCT wallet) FROM hl_whale_positions").fetchone()[0]
            position_count = ctx["conn"].execute("SELECT COUNT(*) FROM hl_whale_positions").fetchone()[0]
            closest = ctx["conn"].execute("SELECT MIN(distance_pct) FROM hl_whale_positions").fetchone()[0]
            # Always SCANNING until Phase 5B strategy plugin exists
            return jsonify({
                "state": "SCANNING",
                "wallet_count": wallet_count,
                "position_count": position_count,
                "closest_distance_pct": round(closest, 2) if closest else None,
            })
        except Exception:
            logger.exception("hl_liquidation_state failed")
            return jsonify({"error": "Internal server error"}), 500

    @bp.get("/api/strategies/<int:strategy_id>/stink-bid-orders")
    def api_stink_bid_orders(strategy_id: int):
        """Return the paper limit order book for a stink bid strategy."""
        conn = ctx["conn"]
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        if row is None:
            return jsonify({"error": "strategy not found"}), 404
        if str(row["strategy_type"] or "") != "stink_bid_v1":
            return jsonify({"error": "not a stink_bid_v1 strategy"}), 400

        orders = conn.execute(
            """SELECT * FROM paper_limit_orders
               WHERE strategy_id = ?
               ORDER BY placed_at DESC""",
            (strategy_id,),
        ).fetchall()

        result = []
        state_counts = {"resting": 0, "filled": 0, "cancelled": 0, "expired": 0}
        for o in orders:
            state = str(o["state"] or "")
            state_counts[state] = state_counts.get(state, 0) + 1
            oid = int(o["order_id"] or 0)
            fills = conn.execute(
                """SELECT * FROM paper_limit_fills
                   WHERE order_id = ?
                   ORDER BY created_at DESC""",
                (oid,),
            ).fetchall()
            result.append({
                "order_id": oid,
                "condition_id": str(o["condition_id"] or ""),
                "market_title": str(o["market_title"] or ""),
                "bid_price": float(o["bid_price"] or 0),
                "state": state,
                "tour": str(o["tour"] or ""),
                "player1": str(o["player1"] or ""),
                "player2": str(o["player2"] or ""),
                "event_slug": str(o["event_slug"] or ""),
                "favorite_token_label": str(o["favorite_token_label"] or ""),
                "favorite_price_at_entry": float(o["favorite_price_at_entry"] or 0),
                "placed_at": str(o["placed_at"] or ""),
                "closed_at": str(o["closed_at"] or ""),
                "fills": [
                    {
                        "fill_id": int(f["fill_id"] or 0),
                        "fill_label": str(f["fill_label"] or ""),
                        "trade_price": float(f["trade_price"] or 0),
                        "trade_size": float(f["trade_size"] or 0),
                        "bid_price": float(f["bid_price"] or 0),
                        "slippage": float(f["slippage"] or 0),
                        "trade_ts": str(f["trade_ts"] or ""),
                    }
                    for f in fills
                ],
            })

        return jsonify({
            "orders": result,
            "summary": {
                "total": len(result),
                **state_counts,
            },
        })

    return bp

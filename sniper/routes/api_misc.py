import datetime as dt
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request

from db import (
    ensure_paper_trades_disqualification_columns,
    ensure_paper_trades_duplicate_of_column,
    ensure_paper_trades_slippage_columns,
    ensure_strategy_positions_live_price_columns,
    paper_trade_gate_status,
)
from sniper.settings.web import PROOF_MIN_WIN_RATE
from sniper.routes.contexts import ApiMiscContext


def _updated_at() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def create_api_misc_blueprint(ctx: ApiMiscContext) -> Blueprint:
    bp = Blueprint("api_misc", __name__)

    def _paper_signal_current_price(
        wallet: str,
        condition_id: str,
        outcome: str,
        position_signal_id: Any = None,
    ) -> float | None:
        conn = ctx["conn"]
        if position_signal_id:
            row = conn.execute(
                "SELECT current_price FROM position_signals WHERE rowid = ? LIMIT 1",
                (position_signal_id,),
            ).fetchone()
            if row and row["current_price"] is not None:
                return float(row["current_price"])

        row = conn.execute(
            """
            SELECT current_price
            FROM position_signals
            WHERE wallet = ?
              AND condition_id = ?
              AND LOWER(outcome) = LOWER(?)
            ORDER BY latest_trade_ts DESC
            LIMIT 1
            """,
            (wallet, condition_id, outcome),
        ).fetchone()
        if row and row["current_price"] is not None:
            return float(row["current_price"])
        return None

    @bp.delete("/api/trades/<path:tx_hash>")
    def api_delete_trade(tx_hash: str):
        result = ctx["delete_trade_and_tombstone"](tx_hash, source="/api/trades")
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "delete failed")}), int(result.get("status_code", 400))
        return jsonify(result)

    @bp.delete("/api/suspicious-trades/<path:tx_hash>")
    def api_delete_suspicious_trade(tx_hash: str):
        result = ctx["delete_trade_and_tombstone"](tx_hash, source="/api/suspicious-trades")
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "delete failed")}), int(result.get("status_code", 400))
        return jsonify(result)

    @bp.post("/api/dismissed-suspicious-trades")
    def api_dismiss_suspicious_trade():
        payload = request.get_json(silent=True) or {}
        tx_hash = ctx["normalize_tx_hash"](payload.get("transaction_hash"))
        if not tx_hash:
            return jsonify({"error": "transaction_hash is required"}), 400
        result = ctx["delete_trade_and_tombstone"](tx_hash, source="/api/dismissed-suspicious-trades")
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "delete failed")}), int(result.get("status_code", 400))
        return jsonify(result)

    @bp.delete("/api/dismissed-suspicious-trades/<path:tx_hash>")
    def api_undismiss_suspicious_trade(tx_hash: str):
        normalized = ctx["normalize_tx_hash"](tx_hash)
        if not normalized:
            return jsonify({"error": "transaction_hash is required"}), 400
        removed = ctx["undismiss_suspicious_trade"](ctx["conn"], normalized)
        return jsonify({"ok": True, "transaction_hash": normalized, "removed": bool(removed)})

    @bp.delete("/api/dismissed-suspicious-trades")
    def api_clear_dismissed_suspicious_trades():
        removed = ctx["clear_dismissed_suspicious_trades"](ctx["conn"])
        return jsonify({"ok": True, "removed": removed})

    @bp.get("/api/ignored-wallets")
    def api_ignored_wallets():
        rows = ctx["ignored_wallets"](ctx["conn"])
        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        items: List[Dict[str, Any]] = []
        for row in rows:
            wallet = ctx["normalize_wallet"](row["wallet"])
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet)
            items.append(
                {
                    "wallet": wallet,
                    "username": identity["username"],
                    "pseudonym": identity["pseudonym"],
                    "profile_image": identity["profile_image"],
                    "reason": row["reason"] or "",
                    "ignored_ts": row["ignored_ts"],
                    "ignored_at": ctx["format_unix"](row["ignored_ts"]),
                }
            )
        return jsonify({"count": len(items), "updated_at": _updated_at(), "items": items})

    # -----------------------------------------------------------------------
    # Blocked wallets — hard permanent ban
    # -----------------------------------------------------------------------

    @bp.get("/api/blocked-wallets")
    def api_blocked_wallets():
        rows = ctx["blocked_wallets_list"](ctx["conn"])
        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        items = []
        for row in rows:
            wallet = ctx["normalize_wallet"](row["wallet"])
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet)
            items.append({
                "wallet": wallet,
                "username": identity["username"],
                "pseudonym": identity["pseudonym"],
                "profile_image": identity["profile_image"],
                "reason": row["reason"] or "",
                "blocked_ts": row["blocked_ts"],
                "blocked_by": row["blocked_by"],
                "blocked_at": ctx["format_unix"](row["blocked_ts"]),
            })
        return jsonify({"count": len(items), "updated_at": _updated_at(), "items": items})

    @bp.post("/api/blocked-wallets")
    def api_block_wallet():
        payload = request.get_json(silent=True) or {}
        wallet = ctx["normalize_wallet"](payload.get("wallet"))
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        reason = str(payload.get("reason") or "blocked by user").strip()
        result = ctx["block_wallet"](ctx["conn"], wallet, reason)
        if not result.get("ok"):
            return jsonify({"error": result.get("error", "block failed")}), 400
        return jsonify(result)

    @bp.delete("/api/blocked-wallets/<wallet>")
    def api_unblock_wallet(wallet: str):
        normalized = ctx["normalize_wallet"](wallet)
        if not normalized:
            return jsonify({"error": "wallet is required"}), 400
        conn = ctx["conn"]
        conn.execute("DELETE FROM blocked_wallets WHERE wallet=?", (normalized,))
        conn.commit()
        # Restore classification to empty so it gets re-analyzed on next cycle
        conn.execute(
            "UPDATE user_analysis SET classification=NULL, reason='' WHERE wallet=? AND classification='blocked'",
            (normalized,),
        )
        conn.commit()
        return jsonify({"ok": True, "wallet": normalized})

    @bp.post("/api/ignored-wallets")
    def api_ignore_wallet():
        payload = request.get_json(silent=True) or {}
        wallet = ctx["normalize_wallet"](payload.get("wallet"))
        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        reason = str(payload.get("reason") or "").strip()
        ctx["ignore_wallet"](ctx["conn"], wallet, reason)
        return jsonify({"ok": True, "wallet": wallet, "reason": reason})

    @bp.delete("/api/ignored-wallets/<wallet>")
    def api_unignore_wallet(wallet: str):
        normalized = ctx["normalize_wallet"](wallet)
        if not normalized:
            return jsonify({"error": "wallet is required"}), 400
        removed = ctx["unignore_wallet"](ctx["conn"], normalized)
        return jsonify({"ok": True, "wallet": normalized, "removed": bool(removed)})

    @bp.delete("/api/ignored-wallets")
    def api_clear_ignored_wallets():
        removed = ctx["clear_ignored_wallets"](ctx["conn"])
        return jsonify({"ok": True, "removed": removed})

    @bp.get("/api/paper-trades")
    def api_paper_trades():
        """Return paper trade history grouped by signal_type with stats.

        Optional query params:
          limit  — max rows to return (default 2000, max 20000)
          offset — rows to skip for pagination (default 0)
          status — filter by status: 'open', 'closed', or 'all' (default 'all')
        """
        conn = ctx["conn"]
        ensure_paper_trades_slippage_columns(conn)
        ensure_paper_trades_duplicate_of_column(conn)
        ensure_paper_trades_disqualification_columns(conn)
        ensure_strategy_positions_live_price_columns(conn)
        raw_limit = request.args.get("limit", "2000")
        try:
            limit = max(1, min(20000, int(raw_limit)))
        except (ValueError, TypeError):
            limit = 2000
        raw_offset = request.args.get("offset", "0")
        try:
            offset = max(0, int(raw_offset))
        except (ValueError, TypeError):
            offset = 0
        status_filter = request.args.get("status", "all").lower()
        if status_filter not in ("open", "closed"):
            status_filter = "all"
        if status_filter == "all":
            rows = conn.execute("""
                SELECT trade_id, wallet, condition_id, outcome, market_title, direction,
                       entry_price, current_price, exit_price, size_usdc, pnl_usdc, roi_percent,
                       signal_type, exit_classification, exit_reason,
                       signal_insider_score, wallet_win_rate, signal_entry_price, slippage_pct,
                       opened_at, closed_at, status, position_signal_id,
                       signal_disqualified_at, signal_disqualification_reason,
                       duplicate_of
                FROM paper_trades
                ORDER BY opened_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT trade_id, wallet, condition_id, outcome, market_title, direction,
                       entry_price, current_price, exit_price, size_usdc, pnl_usdc, roi_percent,
                       signal_type, exit_classification, exit_reason,
                       signal_insider_score, wallet_win_rate, signal_entry_price, slippage_pct,
                       opened_at, closed_at, status, position_signal_id,
                       signal_disqualified_at, signal_disqualification_reason,
                       duplicate_of
                FROM paper_trades
                WHERE status = ?
                ORDER BY opened_at DESC
                LIMIT ? OFFSET ?
            """, (status_filter, limit, offset)).fetchall()

        items = [dict(r) for r in rows]
        wallets = [ctx["normalize_wallet"](row.get("wallet")) for row in items if row.get("wallet")]
        profiles_by_wallet = ctx["wallet_profiles_map"](conn, wallets) if wallets else {}
        for row in items:
            if row.get("status") == "open":
                live_price = _paper_signal_current_price(
                    str(row.get("wallet") or ""),
                    str(row.get("condition_id") or ""),
                    str(row.get("outcome") or ""),
                    row.get("position_signal_id"),
                )
                if live_price is not None and live_price > 0:
                    row["current_price"] = live_price
            wallet = ctx["normalize_wallet"](row.get("wallet"))
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet) if wallet else {
                "username": None,
                "pseudonym": None,
                "profile_image": None,
            }
            row["username"] = identity.get("username")
            row["pseudonym"] = identity.get("pseudonym")
            row["profile_image"] = identity.get("profile_image")
            row.pop("position_signal_id", None)

        # Aggregate stats by signal_type — exclude restart-duplicate trades from all counts
        # (duplicate_of IS NOT NULL means the row is a soft-marked duplicate preserved for audit)
        items_for_stats = [r for r in items if not r.get("duplicate_of")]

        from collections import defaultdict
        groups = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0,
                                       "resolution_wins": 0, "resolution_losses": 0,
                                       "early_exits": 0, "source_exited": 0,
                                       "total_pnl": 0.0})
        for row in items_for_stats:
            st = row.get("signal_type") or "unknown"
            g = groups[st]
            g["count"] += 1
            pnl = row.get("pnl_usdc") or 0.0
            g["total_pnl"] += pnl
            ec = row.get("exit_classification") or ""
            if ec == "resolution_win":
                g["resolution_wins"] += 1
                g["wins"] += 1
            elif ec == "resolution_loss":
                g["resolution_losses"] += 1
                g["losses"] += 1
            elif ec == "early_exit":
                g["early_exits"] += 1
                if pnl > 0:
                    g["wins"] += 1
                else:
                    g["losses"] += 1
            elif ec == "source_exited":
                g["source_exited"] += 1
                if pnl > 0:
                    g["wins"] += 1
                else:
                    g["losses"] += 1

        stats = []
        for st, g in groups.items():
            resolved = g["resolution_wins"] + g["resolution_losses"]
            stats.append({
                "signal_type": st,
                "count": g["count"],
                "win_rate": round(g["wins"] / max(g["wins"] + g["losses"], 1), 3),
                "resolution_win_rate": round(g["resolution_wins"] / max(resolved, 1), 3),
                "resolution_wins": g["resolution_wins"],
                "resolution_losses": g["resolution_losses"],
                "early_exits": g["early_exits"],
                "source_exited": g["source_exited"],
                "total_pnl": round(g["total_pnl"], 2),
                "avg_pnl": round(g["total_pnl"] / max(g["count"], 1), 2),
            })

        # Overall stats — count all terminal statuses, not just 'closed'
        TERMINAL_STATUSES = {
            "closed", "manual", "resolution_win", "resolution_loss",
            "early_exit", "source_exited", "stale",
        }
        total_closed = [r for r in items_for_stats if r.get("status") in TERMINAL_STATUSES]
        total_pnl = sum(r.get("pnl_usdc") or 0.0 for r in total_closed)
        wins = sum(1 for r in total_closed if (r.get("pnl_usdc") or 0) > 0)
        closed_count = len(total_closed)
        roi_values = [r.get("roi_percent") for r in total_closed if r.get("roi_percent") is not None]
        avg_roi = round(sum(roi_values) / len(roi_values), 2) if roi_values else None

        gate = paper_trade_gate_status(conn)
        gate_ready = gate["gate_ready"]
        GATE_THRESHOLD = gate["gate_threshold"]

        # Pipeline status: how many qualifying signals exist right now?
        try:
            # Mirrors paper_trader.py entry gates exactly:
            #   insider >= PAPER_ENTRY_MIN_INSIDER_SCORE (55)
            #   speculator < 70
            #   current_price in [0.25, 0.75]  (same as MIN/MAX_ENTRY_PRICE)
            ps_row = conn.execute("""
                SELECT
                    COUNT(*) as total_open,
                    SUM(CASE WHEN position_insider_score >= 55
                              AND COALESCE(position_speculator_score, 0) < 70
                              AND current_price >= 0.25
                              AND current_price <= 0.75
                         THEN 1 ELSE 0 END) as qualifying,
                    MAX(position_insider_score) as max_insider
                FROM position_signals
                WHERE is_open = 1
            """).fetchone()
            pipeline = {
                "open_signals": ps_row["total_open"],
                "qualifying_signals": ps_row["qualifying"],
                "max_insider_score": ps_row["max_insider"],
            }
        except Exception:
            pipeline = {"open_signals": 0, "qualifying_signals": 0, "max_insider_score": None}

        # Total row count (unfiltered by pagination) for pagination metadata
        if status_filter == "all":
            total_rows = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        else:
            total_rows = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status = ?", (status_filter,)
            ).fetchone()[0]

        return jsonify({
            "count": len(items),
            "total": total_rows,
            "limit": limit,
            "offset": offset,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(wins / max(closed_count, 1), 3),
            "avg_roi": avg_roi,
            "open_count": sum(1 for r in items if r.get("status") == "open"),
            "closed_count": closed_count,
            "gate_threshold": GATE_THRESHOLD,
            "gate_ready": gate_ready,
            "gate_detail": {
                "win_rate_ok": gate["win_rate"] >= PROOF_MIN_WIN_RATE / 100.0,
                "avg_roi_ok": gate["avg_roi"] >= 10.0,
                "pnl_ok": gate["total_pnl"] > 0,
                "count_ok": gate["closed_count"] >= gate["gate_threshold"],
                "fail_reasons": gate["fail_reasons"],
            },
            "pipeline": pipeline,
            "by_signal_type": stats,
            "items": items,
            "updated_at": _updated_at(),
        })

    @bp.get("/api/all-positions")
    def api_all_positions():
        """Unified view: paper_trades + strategy_positions merged into one list.

        Returns items with a 'source' field: 'paper' (insider copy) or 'strategy' (plugin).
        All open positions from both systems in one endpoint.
        """
        conn = ctx["conn"]
        ensure_paper_trades_slippage_columns(conn)
        ensure_paper_trades_duplicate_of_column(conn)
        ensure_paper_trades_disqualification_columns(conn)
        ensure_strategy_positions_live_price_columns(conn)

        # --- 1. Paper trades (insider copy) ---
        pt_rows = conn.execute("""
            SELECT trade_id, wallet, condition_id, outcome, market_title, direction,
                   entry_price, current_price, exit_price, size_usdc, pnl_usdc, roi_percent,
                   signal_type, exit_classification, exit_reason,
                   signal_insider_score, slippage_pct,
                   opened_at, closed_at, status,
                   position_signal_id, duplicate_of
            FROM paper_trades
            ORDER BY opened_at DESC
        """).fetchall()

        items: List[Dict] = []
        for r in pt_rows:
            row = dict(r)
            row["source"] = "paper"
            row["strategy_label"] = row.get("signal_type") or "insider_copy"
            # Live price for open trades
            if row.get("status") == "open":
                live = _paper_signal_current_price(
                    str(row.get("wallet") or ""),
                    str(row.get("condition_id") or ""),
                    str(row.get("outcome") or ""),
                    row.get("position_signal_id"),
                )
                if live is not None and live > 0:
                    row["current_price"] = live
            row.pop("position_signal_id", None)
            items.append(row)

        # --- 2. Strategy positions (autonomous plugins) ---
        # Include current_price from DB (updated by strategy_runner every ~60s)
        sp_rows = conn.execute("""
            SELECT sp.position_id, sp.condition_id, sp.outcome, sp.state,
                   sp.avg_entry_price, sp.entry_shares, sp.realized_pnl,
                   sp.avg_exit_price, sp.opened_ts, sp.closed_ts,
                   sp.current_price AS db_current_price,
                   s.id AS strat_id, s.label AS strat_label, s.strategy_type,
                   mm.title AS market_title
            FROM strategy_positions sp
            JOIN strategies s ON CAST(sp.strategy_id AS TEXT) = CAST(s.id AS TEXT)
            LEFT JOIN market_metadata mm ON mm.condition_id = sp.condition_id
            ORDER BY sp.opened_ts DESC
        """).fetchall()

        for r in sp_rows:
            sp = dict(r)
            entry_price = float(sp.get("avg_entry_price") or 0)
            shares = float(sp.get("entry_shares") or 0)
            size_usdc = round(entry_price * shares, 2)
            realized_pnl = float(sp.get("realized_pnl") or 0)
            state = sp.get("state", "open")
            # Map state to paper status vocabulary
            if state == "open":
                status = "open"
                # Use DB price (strategy_runner keeps it fresh ~60s)
                db_price = sp.get("db_current_price")
                current_price = float(db_price) if db_price is not None else entry_price
                exit_price = None
                pnl_usdc = None
                roi_percent = None
            else:
                status = "closed"
                exit_price = float(sp.get("avg_exit_price") or 0) or None
                current_price = exit_price
                pnl_usdc = realized_pnl
                roi_percent = round(realized_pnl / size_usdc * 100, 2) if size_usdc > 0 else None

            opened_at = (
                dt.datetime.fromtimestamp(int(sp["opened_ts"]), tz=dt.timezone.utc).isoformat()
                if sp.get("opened_ts") else None
            )
            closed_at = (
                dt.datetime.fromtimestamp(int(sp["closed_ts"]), tz=dt.timezone.utc).isoformat()
                if sp.get("closed_ts") else None
            )

            items.append({
                "source": "strategy",
                "trade_id": f"sp_{sp['position_id']}",
                "wallet": "autonomous",
                "condition_id": sp.get("condition_id"),
                "outcome": sp.get("outcome"),
                "market_title": sp.get("market_title") or sp.get("condition_id", "")[:16] + "…",
                "direction": sp.get("outcome", "").upper(),
                "entry_price": entry_price,
                "current_price": current_price,
                "exit_price": exit_price,
                "size_usdc": size_usdc,
                "pnl_usdc": pnl_usdc,
                "roi_percent": roi_percent,
                "signal_type": sp.get("strategy_type"),
                "strategy_label": sp.get("strat_label"),
                "exit_classification": None,
                "exit_reason": None,
                "signal_insider_score": None,
                "slippage_pct": None,
                "opened_at": opened_at,
                "closed_at": closed_at,
                "status": status,
                "duplicate_of": None,
            })

        # Sort: open first, then by timestamp desc
        def _sort_key(item):
            is_open = 1 if item.get("status") == "open" else 0
            ts = item.get("opened_at") or ""
            return (-is_open, ts)
        items.sort(key=lambda x: (0 if x.get("status") == "open" else 1, -(
            int(dt.datetime.fromisoformat(x["opened_at"].replace("Z", "+00:00")).timestamp())
            if x.get("opened_at") else 0
        )))

        open_items = [i for i in items if i.get("status") == "open"]
        closed_items = [i for i in items if i.get("status") != "open"]

        # Aggregate stats (so client doesn't need /api/paper-trades for stats)
        wins = 0
        losses = 0
        total_pnl = 0.0
        total_roi_sum = 0.0
        roi_count = 0
        for it in items:
            pnl = it.get("pnl_usdc") or 0.0
            ec = it.get("exit_classification") or ""
            st = it.get("status") or ""
            if ec == "resolution_win" or (st != "open" and pnl > 0):
                wins += 1
            if ec == "resolution_loss" or (st != "open" and pnl < 0):
                losses += 1
            if st != "open":
                total_pnl += pnl
                roi = it.get("roi_percent")
                if roi is not None:
                    total_roi_sum += roi
                    roi_count += 1

        closed_count = len(closed_items)
        avg_roi = round(total_roi_sum / roi_count, 2) if roi_count > 0 else None
        win_rate = round(wins / max(wins + losses, 1), 3)

        # Pipeline stats (qualifying signals ready for paper trading)
        try:
            ps_row = conn.execute("""
                SELECT
                    COUNT(*) as total_open,
                    SUM(CASE WHEN position_insider_score >= 55
                              AND COALESCE(position_speculator_score, 0) < 70
                              AND current_price >= 0.25
                              AND current_price <= 0.75
                         THEN 1 ELSE 0 END) as qualifying,
                    MAX(position_insider_score) as max_insider
                FROM position_signals
                WHERE is_open = 1
            """).fetchone()
            pipeline = {
                "open_signals": ps_row["total_open"],
                "qualifying_signals": ps_row["qualifying"],
                "max_insider_score": ps_row["max_insider"],
            }
        except Exception:
            pipeline = {"open_signals": 0, "qualifying_signals": 0, "max_insider_score": None}

        stats = {
            "count": len(items),
            "open_count": len(open_items),
            "closed_count": closed_count,
            "total_pnl": round(total_pnl, 2),
            "win_rate": win_rate,
            "avg_roi": avg_roi,
            "pipeline": pipeline,
        }

        return jsonify({
            "count": len(items),
            "items": items,
            "stats": stats,
            "open_count": len(open_items),
            "closed_count": closed_count,
            "total_pnl": round(total_pnl, 2),
            "win_rate": win_rate,
            "avg_roi": avg_roi,
            "pipeline": pipeline,
            "updated_at": _updated_at(),
        })

    @bp.post("/api/paper-trades")
    def api_create_paper_trade():
        conn = ctx["conn"]
        ensure_paper_trades_slippage_columns(conn)
        ensure_paper_trades_duplicate_of_column(conn)
        payload = request.get_json(silent=True) or {}

        def _safe_float(value: Any) -> float:
            fn = ctx.get("safe_float")
            if callable(fn):
                try:
                    return float(fn(value))
                except Exception:
                    return 0.0
            try:
                if value is None:
                    return 0.0
                text = str(value).strip()
                if text == "":
                    return 0.0
                return float(text)
            except Exception:
                return 0.0

        wallet = ctx["normalize_wallet"](payload.get("wallet"))
        condition_id = str(payload.get("condition_id") or "").strip()
        # Normalize outcome to lowercase (consistent with paper_trader._normalize_outcome_key).
        # Binary markets use "yes"/"no"; multi-outcome markets use the outcome name (e.g. "warriors").
        outcome = str(payload.get("outcome") or "").strip().lower()
        entry_price = _safe_float(payload.get("entry_price"))
        size_usdc = _safe_float(payload.get("size_usdc"))

        if not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        if not condition_id:
            return jsonify({"error": "condition_id is required"}), 400
        if not outcome:
            return jsonify({"error": "outcome is required"}), 400
        if entry_price <= 0:
            return jsonify({"error": "entry_price must be > 0"}), 400
        if size_usdc <= 0:
            return jsonify({"error": "size_usdc must be > 0"}), 400

        client_request_id = str(payload.get("client_request_id") or "").strip()

        signal_entry_price_raw = payload.get("signal_entry_price")
        signal_entry_price = _safe_float(signal_entry_price_raw)
        if signal_entry_price <= 0:
            signal_entry_price = None

        slippage_pct = None
        if signal_entry_price is not None:
            slippage_pct = round(((entry_price - signal_entry_price) / signal_entry_price) * 100, 2)

        shares = size_usdc / entry_price if entry_price > 0 else 0.0
        market_title = str(payload.get("title") or "").strip()
        signal_type = str(payload.get("signal_type") or "manual_copy").strip() or "manual_copy"
        position_value = _safe_float(payload.get("position_value"))
        if position_value <= 0:
            position_value = size_usdc

        existing_open = conn.execute(
            """
            SELECT trade_id
            FROM paper_trades
            WHERE wallet = ? AND condition_id = ? AND UPPER(outcome) = UPPER(?) AND status = 'open'
            LIMIT 1
            """,
            (wallet, condition_id, outcome),
        ).fetchone()
        if existing_open:
            return jsonify({
                "ok": True,
                "trade_id": existing_open["trade_id"],
                "deduplicated": True,
            })

        # Race-safe idempotency: INSERT OR IGNORE + unique index on
        # (wallet, condition_id, outcome, client_request_id) WHERE client_request_id IS NOT NULL.
        # If the unique index rejects the row, conn.total_changes stays the same.
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO paper_trades (
                wallet, condition_id, outcome, market_title, direction,
                entry_price, current_price, size_usdc, shares,
                position_value_at_entry, stop_loss_price, take_profit_price,
                signal_type, signal_insider_score, signal_speculator_score, wallet_win_rate,
                signal_entry_price, slippage_pct, client_request_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                wallet,
                condition_id,
                outcome,
                market_title,
                outcome,
                entry_price,
                entry_price,
                size_usdc,
                shares,
                position_value,
                0.0,
                0.0,
                signal_type,
                0.0,
                0.0,
                0.0,
                signal_entry_price,
                slippage_pct,
                client_request_id or None,
            ),
        )
        conn.commit()

        if cur.rowcount == 0 and client_request_id:
            # Duplicate suppressed by unique index — look up existing row
            existing = conn.execute(
                """SELECT trade_id FROM paper_trades
                   WHERE wallet = ? AND condition_id = ? AND outcome = ?
                     AND client_request_id = ?""",
                (wallet, condition_id, outcome, client_request_id),
            ).fetchone()
            return jsonify({
                "ok": True,
                "trade_id": existing["trade_id"] if existing else None,
                "deduplicated": True,
            })

        return jsonify({"ok": True, "trade_id": cur.lastrowid})

    @bp.post("/api/paper-trades/<int:trade_id>/close")
    def api_manual_close_paper_trade(trade_id: int):
        """Manually close a paper trade position."""
        conn = ctx["conn"]
        row = conn.execute("""
            SELECT trade_id, entry_price, current_price, size_usdc, wallet, condition_id,
                   outcome, position_signal_id
            FROM paper_trades
            WHERE trade_id = ? AND status = 'open'
        """, (trade_id,)).fetchone()

        if not row:
            return jsonify({"error": "No open paper trade found"}), 404

        live_price = _paper_signal_current_price(
            str(row["wallet"] or ""),
            str(row["condition_id"] or ""),
            str(row["outcome"] or ""),
            row["position_signal_id"],
        )
        fallback_price = float(row["current_price"]) if row["current_price"] is not None else float(row["entry_price"])
        exit_price = live_price if live_price is not None and live_price > 0 else fallback_price
        entry_price = float(row["entry_price"])
        size_usdc = float(row["size_usdc"])
        shares = size_usdc / entry_price if entry_price > 0 else 0
        pnl = (exit_price - entry_price) * shares
        roi = ((exit_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

        conn.execute("""
            UPDATE paper_trades
            SET status = 'closed', current_price = ?, exit_price = ?, exit_classification = 'manual',
                exit_reason = 'manual_close', pnl_usdc = ?, roi_percent = ?,
                closed_at = CURRENT_TIMESTAMP
            WHERE trade_id = ?
        """, (exit_price, exit_price, round(pnl, 4), round(roi, 2), trade_id))
        conn.commit()

        return jsonify({"ok": True, "trade_id": trade_id, "pnl_usdc": round(pnl, 4),
                        "exit_price": exit_price, "exit_classification": "manual"})

    return bp

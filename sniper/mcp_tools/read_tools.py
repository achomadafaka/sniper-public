"""Read-only MCP tools for Sniper. Registered via register_read_tools()."""
from __future__ import annotations

import os
import time

from db import (
    get_db_path,
    get_readonly_connection,
    get_wallet_analysis,
    paper_trades_with_scores,
    position_signals as db_position_signals,
    wallet_followable_leaderboard_rows as db_leaderboard,
    wallet_top_domains,
)
from db import get_market_metadata as db_get_market_metadata
from scripts.ops.health_check import get_health_summary, get_telegram_health_summary
from sniper.mcp_tools.schemas import make_envelope


def _row_col(row, col, default=""):
    """Safe column access for sqlite3.Row (no .get())."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return default


def _db_file_mtime(db_path: str | None = None) -> float | None:
    """Return mtime of DB file as a timestamp, or None."""
    path = db_path or get_db_path()
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


_ALLOWED_TABLES = frozenset({
    "low_buy_trades", "user_analysis", "position_signals", "paper_trades",
    "market_metadata", "alerts_sent", "followed_positions", "signal_history",
    "score_history", "strategies", "strategy_positions", "candidate_users",
    "flagged_users", "watchlist_wallets", "my_trades", "analysis_runs",
})
_ALLOWED_COLUMNS = frozenset({
    "timestamp", "analyzed_ts", "opened_at", "closed_at", "sent_at",
    "created_at", "updated_at", "committed_ts", "started_ts",
})


def _max_table_ts(conn, table: str, column: str) -> float | None:
    """Return MAX(column) from table as a float timestamp when available."""
    if table not in _ALLOWED_TABLES or column not in _ALLOWED_COLUMNS:
        return None
    try:
        row = conn.execute(f"SELECT MAX({column}) AS ts FROM {table}").fetchone()
    except Exception:
        return None
    if not row or not row["ts"]:
        return None
    try:
        return float(row["ts"])
    except Exception:
        return None


def runtime_health(*, db_path: str | None = None) -> dict:
    """Internal implementation for get_runtime_health tool."""
    path = db_path or get_db_path()
    summary = get_health_summary(path)
    return make_envelope(
        summary, data_source=path, last_write_ts=_db_file_mtime(path),
    )


def current_signals(
    *,
    min_insider_score: float = 55,
    max_entry_price: float = 0.70,
    open_only: bool = True,
    limit: int = 100,
    conn=None,
    db_path: str | None = None,
) -> dict:
    """Internal implementation for get_current_signals tool."""
    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        rows = db_position_signals(
            conn,
            min_insider_score=min_insider_score,
            max_entry_price=max_entry_price,
            open_only=open_only,
            limit=limit,
        )

        # Get freshness from latest analyzed_ts
        freshness_ts = None
        try:
            row = conn.execute(
                "SELECT MAX(analyzed_ts) as ts FROM position_signals WHERE is_open=1"
            ).fetchone()
            if row and row["ts"]:
                freshness_ts = float(row["ts"])
        except Exception:
            pass

        signals = []
        for r in rows:
            entry_price = float(r["entry_price"] or 0)
            title = r["title"] or _row_col(r, "market_title", "")
            signals.append({
                "wallet": r["wallet"],
                "condition_id": r["condition_id"],
                "outcome": r["outcome"],
                "title": title,
                "entry_price": entry_price,
                "current_price": float(r["current_price"] or 0),
                "position_insider_score": float(r["position_insider_score"] or 0),
                "position_speculator_score": float(r["position_speculator_score"] or 0),
                "position_classification": r["position_classification"],
                "hold_hours": float(r["hold_hours"] or 0),
                "is_open": bool(r["is_open"]),
            })
        return make_envelope(
            {"signals": signals, "count": len(signals)},
            data_source="position_signals",
            last_write_ts=freshness_ts,
        )
    finally:
        if close_conn:
            conn.close()


def wallet_profile(*, wallet: str, conn=None, db_path: str | None = None) -> dict:
    """Internal implementation for get_wallet_profile tool."""
    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        row = get_wallet_analysis(conn, wallet)
        if not row:
            return make_envelope(None, data_source="user_analysis")

        domains = []
        try:
            domain_map = wallet_top_domains(conn, [wallet])
            domains = domain_map.get(wallet.lower(), [])
        except Exception:
            pass

        data = {
            "wallet": row["wallet"],
            "insider_score": float(row["insider_score"] or 0),
            "speculator_score": float(row["speculator_score"] or 0),
            "classification": row["classification"],
            "tier": row["tier"],
            "strategy_group": _row_col(row, "strategy_group", ""),
            "wallet_edge_tier": _row_col(row, "wallet_edge_tier", ""),
            "followability_cached": _row_col(row, "followability_cached", None),
            "win_rate": float(row["win_rate"] or 0),
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "closed_sample_size": int(row["closed_sample_size"] or 0),
            "total_closed_count": int(_row_col(row, "total_closed_count", 0) or 0),
            "pnl": float(row["pnl"] or 0),
            "roi_percent": float(_row_col(row, "roi_percent", 0) or 0),
            "qualifying_pnl": float(_row_col(row, "qualifying_pnl", 0) or 0),
            "position_count": int(row["position_count"] or 0),
            "total_open_value": float(row["total_open_value"] or 0),
            "account_age_days": int(row["account_age_days"] or 0),
            "top_domains": domains,
        }
        analyzed_ts = float(row["analyzed_ts"] or 0) if row["analyzed_ts"] else None
        return make_envelope(data, data_source="user_analysis", last_write_ts=analyzed_ts)
    finally:
        if close_conn:
            conn.close()


def market_context(*, condition_id: str, conn=None, db_path: str | None = None) -> dict:
    """Internal implementation for get_market_context tool."""
    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        meta = db_get_market_metadata(conn, condition_id)
        meta_dict = {}
        if meta:
            meta_dict = {
                "condition_id": condition_id,
                "title": meta["title"],
                "category": _row_col(meta, "category", ""),
                "end_date_iso": _row_col(meta, "end_date_iso", ""),
                "volume_24h": float(_row_col(meta, "volume_24h", 0) or 0),
            }

        # Active signals on this market — per-outcome breakdown
        active_signals = []
        freshness_ts = None
        try:
            sig_rows = conn.execute(
                """SELECT outcome,
                          COUNT(DISTINCT wallet) AS wallet_count,
                          ROUND(AVG(position_insider_score), 1) AS avg_insider_score,
                          ROUND(SUM(entry_notional), 2) AS total_capital,
                          MAX(analyzed_ts) AS latest_ts
                   FROM position_signals
                   WHERE condition_id = ? AND is_open = 1
                   GROUP BY outcome
                   ORDER BY wallet_count DESC""",
                (condition_id,),
            ).fetchall()
            for sr in sig_rows:
                active_signals.append({
                    "outcome": sr["outcome"],
                    "wallet_count": sr["wallet_count"],
                    "avg_insider_score": float(sr["avg_insider_score"] or 0),
                    "total_capital": float(sr["total_capital"] or 0),
                })
                ts = sr["latest_ts"]
                if ts and (freshness_ts is None or float(ts) > freshness_ts):
                    freshness_ts = float(ts)
        except Exception:
            pass

        # Paper trades on this market
        paper_count = 0
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM paper_trades WHERE condition_id = ? AND status != 'open'",
                (condition_id,),
            ).fetchone()
            paper_count = row["cnt"] if row else 0
        except Exception:
            pass

        data = {
            "market": meta_dict or {"condition_id": condition_id, "title": "Unknown"},
            "active_signals": active_signals,
            "paper_trade_count": paper_count,
        }
        return make_envelope(data, data_source="market_metadata", last_write_ts=freshness_ts)
    finally:
        if close_conn:
            conn.close()


def paper_performance(
    *,
    min_insider_score: float | None = None,
    status_filter: str | None = None,
    limit: int = 50,
    conn=None,
    db_path: str | None = None,
) -> dict:
    """Internal implementation for get_paper_performance tool."""
    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        trades = paper_trades_with_scores(
            conn,
            min_insider_score=min_insider_score,
            status_filter=status_filter,
            limit=limit,
        )

        # Compute aggregates
        count = len(trades)
        wins = sum(1 for t in trades if (t["pnl_usdc"] or 0) > 0)
        total_pnl = sum(t["pnl_usdc"] or 0 for t in trades)
        rois = [t["roi_percent"] for t in trades if t["roi_percent"] is not None]
        avg_roi = sum(rois) / len(rois) if rois else None
        win_rate = wins / count if count > 0 else None

        # Freshness from most recent closed trade
        fresh_ts = None
        try:
            fresh_row = conn.execute(
                "SELECT MAX(closed_at) as mca FROM paper_trades WHERE status != 'open'"
            ).fetchone()
            if fresh_row and fresh_row["mca"]:
                from datetime import datetime, timezone
                try:
                    dt_obj = datetime.strptime(fresh_row["mca"], "%Y-%m-%d %H:%M:%S")
                    fresh_ts = dt_obj.replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    pass
        except Exception:
            pass

        data = {
            "trades": trades,
            "summary": {
                "count": count,
                "wins": wins,
                "losses": count - wins,
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "total_pnl": round(total_pnl, 2),
                "avg_roi": round(avg_roi, 2) if avg_roi is not None else None,
            },
        }
        return make_envelope(data, data_source="paper_trades", last_write_ts=fresh_ts)
    finally:
        if close_conn:
            conn.close()


def followable_leaderboard(*, limit: int = 20, conn=None, db_path: str | None = None) -> dict:
    """Internal implementation for get_followable_leaderboard tool."""
    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        qualified, filtered, meta = db_leaderboard(conn, limit=limit)

        fresh_ts = None
        try:
            fresh_row = conn.execute(
                "SELECT MAX(analyzed_ts) as mts FROM user_analysis"
            ).fetchone()
            if fresh_row and fresh_row["mts"]:
                fresh_ts = float(fresh_row["mts"])
        except Exception:
            pass

        data = {
            "qualified": qualified,
            "filtered_count": len(filtered),
            "meta": meta,
        }
        return make_envelope(data, data_source="user_analysis", last_write_ts=fresh_ts)
    finally:
        if close_conn:
            conn.close()


def signal_history(
    *,
    wallet: str | None = None,
    condition_id: str | None = None,
    limit: int = 100,
    conn=None,
    db_path: str | None = None,
) -> dict:
    """Internal implementation for get_signal_history tool."""
    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        where = ["1=1"]
        params = []
        if wallet:
            where.append("wallet = ?")
            params.append(wallet.lower())
        if condition_id:
            where.append("condition_id = ?")
            params.append(condition_id)
        params.append(int(limit))

        rows = conn.execute(
            f"""SELECT wallet, condition_id, outcome, entry_price, current_price,
                       position_insider_score, position_speculator_score,
                       position_classification, activity_state, hold_hours,
                       entry_anchor_ts, analyzed_ts
                FROM signal_history
                WHERE {" AND ".join(where)}
                ORDER BY analyzed_ts DESC
                LIMIT ?""",
            params,
        ).fetchall()

        items = []
        for r in rows:
            items.append({
                "wallet": r["wallet"],
                "condition_id": r["condition_id"],
                "outcome": r["outcome"],
                "entry_price": float(r["entry_price"] or 0),
                "current_price": float(r["current_price"] or 0),
                "position_insider_score": float(r["position_insider_score"] or 0),
                "position_speculator_score": float(r["position_speculator_score"] or 0),
                "position_classification": r["position_classification"],
                "activity_state": _row_col(r, "activity_state", ""),
                "hold_hours": float(r["hold_hours"] or 0),
                "entry_anchor_ts": int(r["entry_anchor_ts"] or 0),
                "analyzed_ts": int(r["analyzed_ts"] or 0),
            })

        fresh_ts = None
        if items:
            fresh_ts = float(items[0]["analyzed_ts"]) if items[0]["analyzed_ts"] else None

        return make_envelope(
            {"history": items, "count": len(items)},
            data_source="signal_history",
            last_write_ts=fresh_ts,
        )
    finally:
        if close_conn:
            conn.close()


def _populate_health(result: dict) -> None:
    """Populate health_details and update health criterion in proof bar result."""
    import os
    try:
        health = get_health_summary()
        orphan_check = {c["name"]: c for c in health.get("checks", [])}.get("orphaned_paper_trades", {})
        orphan_ok = orphan_check.get("passed", True)
        orphan_detail = orphan_check.get("detail", "")
        orphan_count = int(orphan_check.get("count", 0) or 0)
        ps = health.get("paper_summary", {})
        open_count = int(ps.get("open_count", 0) or 0)

        telegram_health = get_telegram_health_summary()

        # WAL file size
        db_path = health.get("db_path", "sniper_tracker.db")
        wal_path = str(db_path) + "-wal"
        try:
            wal_mb = os.path.getsize(wal_path) / 1024 / 1024 if os.path.exists(wal_path) else 0.0
        except OSError:
            wal_mb = 0.0
        wal_ok = wal_mb < 200.0

        result["health_details"] = {
            "analyzer": {"status": "ok", "detail": f"{health.get('table_counts', {}).get('user_analysis', 0)} wallets scored", "proof_required": True},
            "paper_trader": {"status": "ok" if orphan_ok else "fail", "detail": f"{open_count} open positions monitored", "proof_required": True},
            "data_integrity": {"status": "ok" if orphan_ok else "fail", "detail": f"Orphaned open trades: {orphan_count}", "proof_required": True},
            "db_size": {"status": "ok", "detail": f"{health.get('db_size_mb', 0)} MB, WAL mode", "proof_required": False},
            "wal": {"status": "ok" if wal_ok else "warn", "detail": f"{wal_mb:.0f} MB", "proof_required": False},
            "alerter": {"status": telegram_health["status"], "detail": telegram_health["detail"], "proof_required": False},
        }

        proof_required_ok = all(
            v["status"] == "ok" for v in result["health_details"].values() if v.get("proof_required")
        )
        result["criteria"]["health"]["value"] = "pass" if proof_required_ok else "fail"
        result["criteria"]["health"]["status"] = "pass" if proof_required_ok else "fail"

        # Advisory warnings: surface for UI display but never block go-live
        any_advisory_warn = any(
            v["status"] != "ok" for v in result["health_details"].values() if not v.get("proof_required")
        )
        result["criteria"]["health"]["advisory_warn"] = any_advisory_warn
    except Exception:
        result["health_details"] = {}
        result["criteria"]["health"]["value"] = "unknown"
        result["criteria"]["health"]["status"] = "fail"

    result["overall"] = "ready" if all(
        c["status"] == "pass" for c in result["criteria"].values()
    ) else "not_ready"


def proof_bar_mcp(*, conn=None, db_path: str | None = None) -> dict:
    """Internal implementation for get_proof_bar_status tool."""
    from db import proof_bar_status
    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        data = proof_bar_status(conn)
        _populate_health(data)
        return make_envelope(data, data_source="proof_bar", last_write_ts=_db_file_mtime(db_path))
    finally:
        if close_conn:
            conn.close()


def classification_diagnostics_mcp(
    *,
    min_insider_score: float = 55.0,
    limit: int = 20,
    conn=None,
    db_path: str | None = None,
) -> dict:
    """Explain why high-insider-score wallets are still classified low_signal."""
    from db import classification_diagnostics

    close_conn = False
    if conn is None:
        conn = get_readonly_connection(db_path)
        close_conn = True
    try:
        data = classification_diagnostics(
            conn,
            min_insider_score=min_insider_score,
            limit=limit,
        )
        return make_envelope(
            data,
            data_source="user_analysis",
            last_write_ts=_max_table_ts(conn, "user_analysis", "analyzed_ts"),
            bias_warning="Classification diagnostics summarize stored analyzer checks. "
                         "They explain why wallets are currently classified low_signal "
                         "but do not prove that thresholds are wrong.",
        )
    finally:
        if close_conn:
            conn.close()


def hl_copy_scorecard(*, holdout: bool = False) -> dict:
    """Internal implementation for get_hl_copy_scorecard tool. Reuses the same honest-scorecard
    builder as the /api/hyperlive/copy-scorecard endpoint (single source of truth)."""
    from sniper.routes.api_hyperlive import _copy_scorecard_payload
    return _copy_scorecard_payload(holdout=holdout)


def register_read_tools(mcp_server):
    """Register all read tools with the MCP server."""

    @mcp_server.tool()
    def get_runtime_health() -> dict:
        """Get Sniper system health: DB size, table row counts, paper trade summary, data integrity checks."""
        return runtime_health()

    @mcp_server.tool()
    def get_current_signals(
        min_insider_score: float = 55,
        max_entry_price: float = 0.70,
        open_only: bool = True,
        limit: int = 100,
    ) -> dict:
        """Get active position signals filtered by insider score and entry price. Returns wallet, market, scores, and hold time for each signal."""
        return current_signals(
            min_insider_score=min_insider_score,
            max_entry_price=max_entry_price,
            open_only=open_only,
            limit=limit,
        )

    @mcp_server.tool()
    def get_wallet_profile(wallet: str) -> dict:
        """Get full wallet analysis: scores, classification, edge tier, win rate (qualifying + total), P&L, domains."""
        return wallet_profile(wallet=wallet)

    @mcp_server.tool()
    def get_market_context(condition_id: str) -> dict:
        """Get market details: title, end date, volume, active insider signals, paper trade count."""
        return market_context(condition_id=condition_id)

    @mcp_server.tool()
    def get_paper_performance(
        min_insider_score: float = 0,
        status_filter: str = "",
        limit: int = 50,
    ) -> dict:
        """Get paper trade performance with optional filters. Returns trade list and aggregate stats (win rate, PnL, ROI)."""
        return paper_performance(
            min_insider_score=min_insider_score or None,
            status_filter=status_filter or None,
            limit=limit,
        )

    @mcp_server.tool()
    def get_followable_leaderboard(limit: int = 20) -> dict:
        """Get ranked wallets by followability score. Returns qualified wallets meeting all gates and count of filtered wallets."""
        return followable_leaderboard(limit=limit)

    @mcp_server.tool()
    def get_hl_copy_scorecard(holdout: bool = False) -> dict:
        """Honest deployable scorecard for the Hyperliquid paper copy book: total-book MTM
        (realized + open marked-to-market, defeats realized-only censoring), bootstrap CIs on the
        edge (is it distinguishable from zero?), concentration (does one trade/wallet carry it?),
        beta-neutral alpha, per-wallet ranking, size-decay, and major/alt + side + consensus
        decomposition. Set holdout=True for out-of-sample (post-freeze) rows only. Read-only."""
        return hl_copy_scorecard(holdout=holdout)

    @mcp_server.tool()
    def get_signal_history(
        wallet: str = "",
        condition_id: str = "",
        limit: int = 100,
    ) -> dict:
        """Get immutable signal history snapshots. Filter by wallet and/or condition_id. Shows how scores evolved over time."""
        return signal_history(
            wallet=wallet or None,
            condition_id=condition_id or None,
            limit=limit,
        )

    @mcp_server.tool()
    def get_proof_bar_status() -> dict:
        """Get proof bar go-live readiness: 5 criteria status, pace, monthly breakdown, and health. Used to check if system is ready for real-money deployment."""
        return proof_bar_mcp()

    @mcp_server.tool()
    def get_classification_diagnostics(
        min_insider_score: float = 55.0,
        limit: int = 20,
    ) -> dict:
        """Explain why high insider-score wallets are still classified low_signal and why active qualifying signals may be dry."""
        return classification_diagnostics_mcp(
            min_insider_score=min_insider_score,
            limit=limit,
        )

    @mcp_server.tool()
    def get_news_briefing(hours: int = 24, limit: int = 20) -> dict:
        """Get intelligence briefing: recent news, trade ideas, cross-layer signals, and market-relevant headlines."""
        conn = get_readonly_connection()
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)

            # Recent news by topic
            news = conn.execute("""
                SELECT title, source, topic_tags, published_at
                FROM news_items
                WHERE published_at > datetime('now', ? || ' hours')
                ORDER BY published_at DESC LIMIT ?
            """, (str(-hours), limit)).fetchall()

            # Open trade ideas
            ideas = conn.execute("""
                SELECT id, market_title, direction, confidence, current_price, reasoning, status
                FROM trade_ideas WHERE status = 'open'
                ORDER BY id DESC LIMIT 10
            """).fetchall()

            # Cross-layer: news topics matching insider signals
            convergence = conn.execute("""
                SELECT ti.market_title, ti.direction, ps.wallet, ps.position_insider_score
                FROM trade_ideas ti
                JOIN position_signals ps ON ti.condition_id = ps.condition_id
                WHERE ti.status = 'open' AND ps.is_open = 1 AND ps.position_insider_score >= 50
                LIMIT 10
            """).fetchall()

            # Topic heat (most active topics in recent news)
            topic_heat = conn.execute("""
                SELECT topic_tags, COUNT(*) cnt
                FROM news_items
                WHERE published_at > datetime('now', ? || ' hours')
                  AND topic_tags IS NOT NULL AND topic_tags != '[]'
                GROUP BY topic_tags ORDER BY cnt DESC LIMIT 10
            """, (str(-hours),)).fetchall()

            return {
                "generated_at": now.isoformat(),
                "data_source": "news_items + trade_ideas + position_signals",
                "data": {
                    "news_count": len(news),
                    "news": [dict(r) for r in news],
                    "trade_ideas": [dict(r) for r in ideas],
                    "convergence_signals": [dict(r) for r in convergence],
                    "topic_heat": [{"topic": r["topic_tags"], "count": r["cnt"]} for r in topic_heat],
                },
            }
        finally:
            conn.close()

    @mcp_server.tool()
    def get_strategy_overview() -> dict:
        """Get all active strategies with current positions, PnL, and performance metrics."""
        conn = get_readonly_connection()
        try:
            strategies = conn.execute("""
                SELECT s.id, s.label, s.strategy_type, s.status, s.trades_triggered,
                       ROUND(s.pnl_usdc, 2) as pnl,
                       (SELECT COUNT(*) FROM strategy_positions sp
                        WHERE CAST(sp.strategy_id AS INTEGER) = s.id AND sp.state = 'open') as open_positions,
                       (SELECT COUNT(*) FROM strategy_positions sp
                        WHERE CAST(sp.strategy_id AS INTEGER) = s.id AND sp.state = 'closed') as closed_positions,
                       (SELECT ROUND(SUM(sp.realized_pnl), 2) FROM strategy_positions sp
                        WHERE CAST(sp.strategy_id AS INTEGER) = s.id AND sp.state = 'closed') as realized_pnl
                FROM strategies s
                WHERE s.status = 'active'
                ORDER BY s.id
            """).fetchall()

            return {
                "generated_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "data": [dict(r) for r in strategies],
            }
        finally:
            conn.close()

    @mcp_server.tool()
    def get_proven_wallets(min_wr: float = 0.70, min_samples: int = 15, limit: int = 20) -> dict:
        """Get wallets with proven track records — high WR, meaningful sample size, positive PnL. These are the best copy targets regardless of insider_score."""
        conn = get_readonly_connection()
        try:
            wallets = conn.execute("""
                SELECT wallet, lb_username, insider_score, win_rate, closed_sample_size,
                       pnl, qualifying_roi, classification, is_bot,
                       (SELECT COUNT(*) FROM position_signals ps
                        WHERE ps.wallet = ua.wallet AND ps.is_open = 1) as open_signals
                FROM user_analysis ua
                WHERE win_rate >= ? AND closed_sample_size >= ? AND pnl > 0
                ORDER BY pnl DESC LIMIT ?
            """, (min_wr, min_samples, limit)).fetchall()

            return {
                "generated_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "data": [dict(r) for r in wallets],
                "count": len(wallets),
            }
        finally:
            conn.close()

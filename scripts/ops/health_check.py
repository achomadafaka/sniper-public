#!/usr/bin/env python3
"""
System health monitor for the current paper-trading schema.

Run this manually or from cron/systemd to verify DB access, market freshness,
pending exits, and paper-trade consistency.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from db import get_db_path


TERMINAL_STATUSES = (
    "closed",
    "resolution_win",
    "resolution_loss",
    "early_exit",
    "source_exited",
    "stale",
)
GATE_THRESHOLD = 50


def _runtime_db_path() -> str:
    return get_db_path()


def _runtime_log_file() -> Path:
    return Path(
        os.getenv(
            "HEALTH_CHECK_LOG_FILE",
            str(Path(__file__).resolve().parent / "logs" / "health_checks.log"),
        )
    )


def get_health_summary(db_path: str | None = None) -> dict:
    """Return a structured health summary dict. Importable by MCP tools."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row

    result = {
        "db_path": path,
        "db_size_mb": round(os.path.getsize(path) / (1024 * 1024), 2) if os.path.exists(path) else 0,
        "table_counts": {},
        "checks": [],
    }

    for table in [
        "low_buy_trades", "user_analysis", "position_signals",
        "paper_trades", "market_metadata", "alerts_sent",
        "followed_positions", "signal_history", "strategies",
    ]:
        _HEALTH_TABLES = frozenset({
            "low_buy_trades", "user_analysis", "position_signals",
            "paper_trades", "market_metadata", "alerts_sent",
            "followed_positions", "signal_history", "strategies",
        })
        if table not in _HEALTH_TABLES:
            result["table_counts"][table] = -1
            continue
        try:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            result["table_counts"][table] = row["cnt"]
        except sqlite3.OperationalError:
            result["table_counts"][table] = -1

    try:
        row = conn.execute(
            "SELECT MAX(timestamp) as latest FROM low_buy_trades"
        ).fetchone()
        result["last_trade_ts"] = row["latest"] if row else None
    except sqlite3.OperationalError:
        result["last_trade_ts"] = None

    try:
        terminal = TERMINAL_STATUSES
        placeholders = ",".join("?" for _ in terminal)
        row = conn.execute(f"""
            SELECT COUNT(*) as total,
                   COALESCE(SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END), 0) as wins,
                   COALESCE(SUM(CASE WHEN pnl_usdc <= 0 THEN 1 ELSE 0 END), 0) as losses,
                   COALESCE(SUM(pnl_usdc), 0) as total_pnl
            FROM paper_trades WHERE status IN ({placeholders})
        """, terminal).fetchone()
        result["paper_summary"] = {
            "closed_count": row["total"] or 0,
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "total_pnl": round(float(row["total_pnl"] or 0), 2),
        }
    except sqlite3.OperationalError:
        result["paper_summary"] = {"closed_count": 0, "wins": 0, "losses": 0, "total_pnl": 0.0}

    try:
        row = conn.execute("SELECT COUNT(*) as open_count FROM paper_trades WHERE status = 'open'").fetchone()
        result["paper_summary"]["open_count"] = row["open_count"] if row else 0
    except sqlite3.OperationalError:
        result["paper_summary"]["open_count"] = 0

    checks = []
    try:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM paper_trades pt
            WHERE pt.status = 'open'
            AND NOT EXISTS (
                SELECT 1 FROM position_signals ps
                WHERE ps.wallet = pt.wallet
                  AND ps.condition_id = pt.condition_id
                  AND LOWER(ps.outcome) = LOWER(pt.outcome)
            )
        """).fetchone()
        orphan_count = row["cnt"] if row else 0
        checks.append({
            "name": "orphaned_paper_trades",
            "passed": orphan_count == 0,
            "count": orphan_count,
            "detail": f"{orphan_count} orphaned open paper trades",
        })
    except sqlite3.OperationalError:
        checks.append({"name": "orphaned_paper_trades", "passed": True, "detail": "table missing"})

    result["checks"] = checks
    conn.close()
    return result


def get_telegram_health_summary(db_path: str | None = None) -> dict:
    """Return advisory Telegram health based on config visibility or recent alert sends."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and token != "YOUR_BOT_TOKEN" and chat_id:
        return {"status": "ok", "detail": "Telegram configured"}

    path = db_path or get_db_path()
    try:
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT MAX(sent_at) AS last_sent_at
            FROM alerts_sent
            WHERE channel = 'telegram'
              AND sent_at >= datetime('now', '-7 days')
            """
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        return {"status": "warn", "detail": f"Telegram status unavailable: {exc}"}

    last_sent_at = str(row["last_sent_at"] or "").strip() if row else ""
    if last_sent_at:
        return {"status": "ok", "detail": f"Recent Telegram alert: {last_sent_at}"}
    return {"status": "warn", "detail": "Check Telegram config"}


def log(msg: str) -> None:
    """Log a timestamped message to stdout and the local health log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    log_file = _runtime_log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_runtime_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type IN ('table', 'view') AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _parse_db_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(" UTC", "+00:00").replace("Z", "+00:00")
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _terminal_placeholders() -> str:
    return ",".join("?" for _ in TERMINAL_STATUSES)


def check_database_connection() -> bool:
    """Verify the main DB is accessible and the current paper_trades table exists."""
    try:
        path = _runtime_db_path()
        conn = sqlite3.connect(path, timeout=30)
        count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        conn.close()
        log(f"✅ Database OK: {count} paper_trades rows ({path})")
        return True
    except Exception as e:
        log(f"❌ Database ERROR: {e}")
        return False


def check_data_integrity() -> bool:
    """Check for obvious inconsistencies in paper-trade rows."""
    conn = _connect()
    issues: list[str] = []

    placeholders = _terminal_placeholders()
    missing_exit = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM paper_trades
        WHERE status IN ({placeholders}) AND exit_price IS NULL
        """,
        TERMINAL_STATUSES,
    ).fetchone()[0]
    if missing_exit > 0:
        issues.append(f"❌ {missing_exit} terminal paper trades missing exit_price")

    extreme_slip = conn.execute(
        """
        SELECT COUNT(*)
        FROM paper_trades
        WHERE status = 'open' AND ABS(COALESCE(slippage_pct, 0)) > 100
        """
    ).fetchone()[0]
    if extreme_slip > 0:
        issues.append(f"⚠️  {extreme_slip} open trades have >100% slippage")

    bad_prices = conn.execute(
        """
        SELECT COUNT(*)
        FROM paper_trades
        WHERE status = 'open'
          AND current_price IS NOT NULL
          AND (current_price < 0 OR current_price > 1.0)
        """
    ).fetchone()[0]
    if bad_prices > 0:
        issues.append(f"❌ {bad_prices} open trades have out-of-range current_price values")

    if _table_exists(conn, "position_signals"):
        orphaned = conn.execute(
            """
            SELECT COUNT(*)
            FROM paper_trades pt
            LEFT JOIN position_signals ps
              ON pt.wallet = ps.wallet
             AND pt.condition_id = ps.condition_id
             AND LOWER(pt.outcome) = LOWER(ps.outcome)
            WHERE pt.status = 'open' AND ps.wallet IS NULL
            """
        ).fetchone()[0]
        if orphaned > 0:
            issues.append(f"⚠️  {orphaned} open trades no longer map to position_signals")
    else:
        issues.append("⚠️  position_signals table missing; orphan checks skipped")

    conn.close()

    if issues:
        for issue in issues:
            log(issue)
    else:
        log("✅ Data integrity OK")

    return not any(issue.startswith("❌") for issue in issues)


def check_exit_processing() -> bool:
    """Check whether resolved source positions still have paper trades marked open."""
    conn = _connect()

    if not _table_exists(conn, "position_signals"):
        conn.close()
        log("⚠️  position_signals table missing; exit processing check skipped")
        return False

    pending = conn.execute(
        """
        SELECT COUNT(*)
        FROM paper_trades pt
        JOIN position_signals ps
          ON pt.wallet = ps.wallet
         AND pt.condition_id = ps.condition_id
         AND LOWER(pt.outcome) = LOWER(ps.outcome)
        WHERE pt.status = 'open' AND ps.is_open = 0
        """
    ).fetchone()[0]

    if pending > 0:
        log(f"⚠️  {pending} exits pending processing")
    else:
        log("✅ No pending exits")

    placeholders = _terminal_placeholders()
    last_exit = conn.execute(
        f"""
        SELECT MAX(closed_at)
        FROM paper_trades
        WHERE status IN ({placeholders})
        """,
        TERMINAL_STATUSES,
    ).fetchone()[0]
    if last_exit:
        log(f"✅ Last terminal exit: {last_exit}")
    else:
        log("⚠️  No terminal exits recorded yet")

    conn.close()
    return pending == 0


def check_price_updates() -> bool:
    """Check if position_signals data is still being refreshed."""
    conn = _connect()

    if not _table_exists(conn, "position_signals"):
        conn.close()
        log("❌ position_signals table missing")
        return False

    last_update = conn.execute("SELECT MAX(updated_at) FROM position_signals").fetchone()[0]
    conn.close()

    last = _parse_db_timestamp(last_update)
    if not last:
        log("❌ No valid price update timestamp")
        return False

    now = datetime.now(timezone.utc)
    minutes_ago = (now - last).total_seconds() / 60
    if minutes_ago > 10:
        log(f"❌ Price data stale: {minutes_ago:.1f} min ago")
        return False

    log(f"✅ Price data fresh: {minutes_ago:.1f} min ago")
    return True


def check_pnl_drift() -> bool:
    """Check stored terminal PnL against a simple entry/exit recomputation."""
    conn = _connect()
    placeholders = _terminal_placeholders()
    rows = conn.execute(
        f"""
        SELECT trade_id, entry_price, exit_price, size_usdc, pnl_usdc
        FROM paper_trades
        WHERE status IN ({placeholders})
          AND entry_price IS NOT NULL
          AND exit_price IS NOT NULL
          AND size_usdc IS NOT NULL
          AND pnl_usdc IS NOT NULL
        ORDER BY COALESCE(closed_at, opened_at) DESC
        LIMIT 25
        """,
        TERMINAL_STATUSES,
    ).fetchall()
    conn.close()

    drift_count = 0
    for row in rows:
        entry = float(row["entry_price"] or 0)
        exit_price = float(row["exit_price"] or 0)
        size = float(row["size_usdc"] or 0)
        stored_pnl = float(row["pnl_usdc"] or 0)
        if entry <= 0 or size <= 0:
            continue
        calc_pnl = ((exit_price - entry) / entry) * size
        if abs(calc_pnl - stored_pnl) > 0.05:
            drift_count += 1

    if drift_count > 0:
        log(f"⚠️  {drift_count} recent terminal trades have PnL drift > $0.05")
    else:
        log("✅ PnL calculations consistent")

    return drift_count == 0


def show_performance_summary() -> None:
    """Show a compact paper-trading summary using the live schema."""
    conn = _connect()

    log("")
    log("📊 PERFORMANCE SUMMARY")
    log("-" * 60)

    placeholders = _terminal_placeholders()
    closed_row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN COALESCE(pnl_usdc, 0) > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(COALESCE(pnl_usdc, 0)) AS pnl,
            AVG(COALESCE(pnl_usdc, 0)) AS avg_pnl,
            AVG(roi_percent) AS avg_roi
        FROM paper_trades
        WHERE status IN ({placeholders})
        """,
        TERMINAL_STATUSES,
    ).fetchone()
    if closed_row and closed_row["total"] > 0:
        win_rate = (closed_row["wins"] / closed_row["total"]) * 100
        log(
            "Closed:  "
            f"{closed_row['total']} trades | {win_rate:.1f}% win | "
            f"${float(closed_row['pnl'] or 0):+.2f} total | "
            f"${float(closed_row['avg_pnl'] or 0):+.2f} avg | "
            f"{float(closed_row['avg_roi'] or 0):+.2f}% ROI"
        )
        gate_ready = closed_row["total"] >= GATE_THRESHOLD and float(closed_row["pnl"] or 0) > 0
        log(
            f"Gate:    {closed_row['total']} / {GATE_THRESHOLD} | "
            f"{'READY' if gate_ready else 'NOT READY'}"
        )
    else:
        log("Closed:  No terminal paper trades yet")

    open_row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(COALESCE(size_usdc, 0)) AS capital,
            AVG(current_price) AS avg_current_price
        FROM paper_trades
        WHERE status = 'open'
        """
    ).fetchone()
    if open_row:
        log(
            "Open:    "
            f"{open_row['total']} trades | "
            f"${float(open_row['capital'] or 0):.2f} deployed | "
            f"{float(open_row['avg_current_price'] or 0):.3f} avg px"
        )

    avg_slip = conn.execute(
        """
        SELECT AVG(slippage_pct)
        FROM paper_trades
        WHERE slippage_pct IS NOT NULL
        """
    ).fetchone()[0]
    log(f"Slippage: {float(avg_slip or 0):+.2f}% avg")

    conn.close()


def main() -> bool:
    log("")
    log("=" * 70)
    log("SYSTEM HEALTH CHECK")
    log("=" * 70)

    checks = [
        ("Database Connection", check_database_connection),
        ("Data Integrity", check_data_integrity),
        ("Exit Processing", check_exit_processing),
        ("Price Updates", check_price_updates),
        ("PnL Drift", check_pnl_drift),
    ]

    results: list[tuple[str, bool]] = []
    for name, check_fn in checks:
        try:
            results.append((name, check_fn()))
        except Exception as e:
            log(f"❌ {name} failed: {e}")
            results.append((name, False))

    show_performance_summary()

    log("")
    log("=" * 70)
    passed = sum(1 for _, result in results if result)
    total = len(results)
    log(f"RESULT: {passed}/{total} checks passed")
    log("=" * 70)
    log("")

    return passed == total


if __name__ == "__main__":
    load_dotenv()
    success = main()
    sys.exit(0 if success else 1)

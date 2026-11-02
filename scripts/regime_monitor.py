#!/usr/bin/env python3
"""
regime_monitor.py — Capital protection via data-grounded circuit breakers and regime detection.

What this actually does:
  1. Circuit breakers on real trade streaks (paper_trades + strategy_positions)
  2. BTC volatility regime for crypto_momentum_v1 only (Binance API, no inference)
  3. Per-wallet degradation for insider_copy_v1 wallets with ≥8 closed trades
  4. Data-gap reporting for strategies without enough resolved trades
  5. Writes strategy_regimes_current to Supabase brain
  6. Sends Telegram only when a status change occurs

Usage:
    python3 scripts/regime_monitor.py              # full run, applies changes
    python3 scripts/regime_monitor.py --dry-run    # evaluate + report, no DB changes
    python3 scripts/regime_monitor.py --status     # current signals, no changes
    python3 scripts/regime_monitor.py --reset 16   # clear regime-pause on strategy 16

Cron: */30 * * * * cd /home/sniper/sniper && .venv/bin/python3 scripts/regime_monitor.py
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

def _load_env() -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

from bot.supabase_helper import upsert_memory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("regime_monitor")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.getenv("DB_PATH", str(ROOT / "sniper_tracker.db"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-5244655612")

# Circuit breaker thresholds — all empirically grounded:
#   DRAWDOWN_THRESHOLD = $50: ~10% of ~$500 bankroll. Hard to justify operating
#     normally when down 10% in a single week.
#   PAPER_LOSS_STREAK = 5: at 51.5% WR (actual), P(5 consecutive L) = 0.485^5 = 2.7%.
#     Unlikely enough to be signal vs noise.
#   CRYPTO_LOSS_STREAK = 4: at 67% WR (actual), P(4 consecutive L) = 0.33^4 = 1.2%.
#     Very unlikely if edge persists.
DRAWDOWN_THRESHOLD_USD = 50.0
PAPER_LOSS_STREAK_THRESHOLD = 5
CRYPTO_LOSS_STREAK_THRESHOLD = 4

# BTC regime thresholds (standard momentum strategy filtering):
#   trending: abs(24h_change) > 2.5% AND realized_vol > 1.5%/4h bar
#   choppy:   abs(24h_change) < 1.0% OR realized_vol < 0.5%/4h bar
BTC_TRENDING_CHANGE_PCT = 2.5
BTC_TRENDING_VOL_PCT = 1.5
BTC_CHOPPY_CHANGE_PCT = 1.0
BTC_CHOPPY_VOL_PCT = 0.5

# Wallet degradation: only evaluate wallets with ≥8 closed trades
WALLET_MIN_TRADES = 8
# Flag as degrading if last-8 WR < 40% AND overall WR was > 55% at some point
WALLET_DEGRADED_LAST8_WR = 0.40
WALLET_DEGRADED_REQUIRED_PRIOR_WR = 0.55
# Or: 4+ consecutive losses at tail regardless of prior WR
WALLET_DEGRADED_STREAK = 4

# Regime-pause tag embedded in ai_analysis so we can distinguish from manual pauses
REGIME_PAUSE_TAG = "regime_paused"

# Strategies we monitor
CRYPTO_STRATEGY_ID = 16          # crypto_momentum_v1 — BTC Binary Momentum
STF_STRATEGY_ID = 7              # insider_copy_v1 — S.T.F. Favorite Compounder

# Strategies that need ≥ N closed trades before regime evaluation
MIN_TRADES_FOR_EVALUATION = 20


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_strategies(conn: sqlite3.Connection) -> list[dict]:
    return _fetch_all(conn, "SELECT id, strategy_type, status, label, wallet, ai_analysis FROM strategies ORDER BY id")


def get_terminal_paper_trades(conn: sqlite3.Connection) -> list[dict]:
    """All closed paper_trades (non-duplicate), ordered by closed_at asc."""
    return _fetch_all(conn, """
        SELECT wallet, status, pnl_usdc, opened_at, closed_at, signal_insider_score
        FROM paper_trades
        WHERE status IN ('resolution_win','resolution_loss','early_exit','source_exited','stale','manual')
          AND duplicate_of IS NULL
        ORDER BY closed_at
    """)


def get_strategy_positions(conn: sqlite3.Connection, strategy_id: int) -> list[dict]:
    """Closed positions for a specific strategy, ordered by closed_ts asc."""
    return _fetch_all(conn, """
        SELECT strategy_id, state, pnl_usdc, opened_ts, closed_ts
        FROM strategy_positions
        WHERE strategy_id = ? AND state = 'closed'
        ORDER BY closed_ts
    """, (str(strategy_id),))


def set_strategy_status(conn: sqlite3.Connection, strategy_id: int, status: str, reason: str, dry_run: bool) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    tag = f"[{REGIME_PAUSE_TAG}:{now_iso}] {reason}"
    if not dry_run:
        conn.execute(
            "UPDATE strategies SET status=?, ai_analysis=?, updated_at=? WHERE id=?",
            (status, tag, now_iso, strategy_id)
        )
        conn.commit()
        logger.info("Strategy %d → %s: %s", strategy_id, status, reason)
    else:
        logger.info("[DRY-RUN] Would set strategy %d → %s: %s", strategy_id, status, reason)


def is_regime_paused(strategy: dict) -> bool:
    """Return True if the current pause was set by regime_monitor (not a manual pause)."""
    ai = strategy.get("ai_analysis") or ""
    return REGIME_PAUSE_TAG in ai


# ---------------------------------------------------------------------------
# Weekly PnL / drawdown
# ---------------------------------------------------------------------------

def compute_weekly_drawdown(trades: list[dict]) -> dict:
    """
    Compute peak-to-current drawdown for the current week (Mon 00:00 UTC to now).
    Returns dict with weekly_pnl, peak_pnl, drawdown, trade_count.
    """
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    week_trades = []
    for t in trades:
        ca = t.get("closed_at", "")
        if not ca:
            continue
        try:
            d = datetime.fromisoformat(str(ca).replace("Z", ""))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if d >= week_start:
                week_trades.append((d, t.get("pnl_usdc") or 0.0))
        except Exception:
            pass

    running = 0.0
    peak = 0.0
    for _, pnl in sorted(week_trades):
        running += pnl
        if running > peak:
            peak = running

    return {
        "weekly_pnl": round(running, 2),
        "peak_pnl": round(peak, 2),
        "drawdown": round(peak - running, 2),
        "trade_count": len(week_trades),
    }


# ---------------------------------------------------------------------------
# Loss streak helpers
# ---------------------------------------------------------------------------

def tail_loss_streak(outcomes: list[str]) -> int:
    """Count consecutive losses at the tail of an outcomes list ('W'/'L')."""
    streak = 0
    for o in reversed(outcomes):
        if o == "L":
            streak += 1
        else:
            break
    return streak


def outcomes_from_trades(trades: list[dict]) -> list[str]:
    """Map closed trades to W/L outcomes.  Zero-pnl scratches are excluded —
    a position that entered and immediately stopped at entry price (common when
    the live price feed fails mid-cycle) is not a loss; counting it as one
    inflates the consecutive-loss streak and mis-fires the circuit breaker.
    """
    result = []
    for t in trades:
        pnl = float(t.get("pnl_usdc") or 0)
        if pnl > 0:
            result.append("W")
        elif pnl < 0:
            result.append("L")
        # pnl == 0: scratch — omit from streak to avoid false circuit-breaker trips
    return result


# ---------------------------------------------------------------------------
# BTC regime (Binance)
# ---------------------------------------------------------------------------

def _binance_get(path: str, timeout: int = 8) -> Any:
    url = f"https://api.binance.com/api/v3/{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get_btc_regime() -> dict:
    """
    Fetch BTC 24h ticker + 12 x 4h klines from Binance.
    Computes:
      - 24h price change %
      - Realized volatility: stdev of 4h returns across 12 bars (48h window)
    Returns:
      {'regime': 'trending'|'choppy'|'moderate', 'change_24h_pct': float,
       'realized_vol_pct': float, 'price': float, 'error': str|None}
    """
    try:
        ticker = _binance_get("ticker/24hr?symbol=BTCUSDT")
        change_24h = float(ticker["priceChangePercent"])
        price = float(ticker["lastPrice"])

        klines = _binance_get("klines?symbol=BTCUSDT&interval=4h&limit=13")
        # Each kline: [open_time, open, high, low, close, ...]
        closes = [float(k[4]) for k in klines]
        returns = [(closes[i] - closes[i - 1]) / closes[i - 1] * 100.0 for i in range(1, len(closes))]
        n = len(returns)
        if n < 2:
            raise ValueError("Not enough klines")
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        realized_vol = math.sqrt(variance)

        if abs(change_24h) > BTC_TRENDING_CHANGE_PCT and realized_vol > BTC_TRENDING_VOL_PCT:
            regime = "trending"
        elif abs(change_24h) < BTC_CHOPPY_CHANGE_PCT or realized_vol < BTC_CHOPPY_VOL_PCT:
            regime = "choppy"
        else:
            regime = "moderate"

        return {
            "regime": regime,
            "change_24h_pct": round(change_24h, 2),
            "realized_vol_pct": round(realized_vol, 3),
            "price": round(price, 0),
            "error": None,
        }
    except Exception as e:
        return {"regime": "unknown", "change_24h_pct": 0.0, "realized_vol_pct": 0.0, "price": 0.0, "error": str(e)}


# ---------------------------------------------------------------------------
# Wallet degradation (insider_copy_v1)
# ---------------------------------------------------------------------------

def get_wallet_regimes(trades: list[dict]) -> dict:
    """
    Evaluate per-wallet trade history from paper_trades terminal rows.
    Only evaluates wallets with >= WALLET_MIN_TRADES closed trades.
    Returns: {wallet_addr: {'status': str, 'total_trades': int, 'total_wr': float,
                             'last_8_wr': float, 'streak': int}}
    """
    by_wallet: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        w = t.get("wallet", "")
        if w:
            by_wallet[w].append(t)

    results = {}
    for wallet, wallet_trades in by_wallet.items():
        n = len(wallet_trades)
        if n < WALLET_MIN_TRADES:
            results[wallet] = {
                "status": "insufficient_data",
                "total_trades": n,
                "total_wr": None,
                "last_8_wr": None,
                "streak": None,
                "note": f"need {WALLET_MIN_TRADES}, have {n}",
            }
            continue

        outcomes_all = outcomes_from_trades(wallet_trades)
        total_wins = outcomes_all.count("W")
        total_wr = total_wins / n

        last8 = wallet_trades[-8:]
        last8_outcomes = outcomes_from_trades(last8)
        last8_wins = last8_outcomes.count("W")
        last8_wr = last8_wins / 8

        streak = tail_loss_streak(outcomes_all)

        # Check degradation conditions
        degrading = False
        degradation_reason = ""

        # Condition 1: last-8 WR < 40% AND was above 55% at some point
        # "was above 55%" = check WR on first half of trades
        first_half = wallet_trades[: n // 2]
        first_half_outcomes = outcomes_from_trades(first_half)
        first_half_wr = first_half_outcomes.count("W") / len(first_half) if first_half else 0
        if last8_wr < WALLET_DEGRADED_LAST8_WR and first_half_wr > WALLET_DEGRADED_REQUIRED_PRIOR_WR:
            degrading = True
            degradation_reason = f"last8_wr={last8_wr:.0%} < {WALLET_DEGRADED_LAST8_WR:.0%}, was {first_half_wr:.0%}"

        # Condition 2: 4+ consecutive losses
        if streak >= WALLET_DEGRADED_STREAK:
            degrading = True
            degradation_reason = (degradation_reason + "; " if degradation_reason else "") + f"{streak} consecutive losses"

        if degrading:
            status = "degrading"
        elif last8_wr >= 0.55:
            status = "hot"
        elif last8_wr < 0.40:
            status = "cold"
        else:
            status = "moderate"

        results[wallet] = {
            "status": status,
            "total_trades": n,
            "total_wr": round(total_wr, 3),
            "last_8_wr": round(last8_wr, 3),
            "streak": streak,
            "degradation_reason": degradation_reason if degrading else None,
        }

    return results


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, skipping")
        return False
    try:
        import urllib.parse
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Brain write
# ---------------------------------------------------------------------------

def write_brain(report: dict) -> None:
    content = json.dumps(report, indent=2, default=str)
    upsert_memory(
        type="project",
        name="strategy_regimes_current",
        description=f"Regime monitor snapshot {report.get('timestamp', '')[:16]}",
        content=content,
        source="vps",
    )
    logger.info("Brain updated: strategy_regimes_current")


# ---------------------------------------------------------------------------
# Core evaluation
# ---------------------------------------------------------------------------

def evaluate(dry_run: bool = False, status_only: bool = False) -> dict:
    conn = _get_conn()
    strategies = get_strategies(conn)
    strat_by_id = {s["id"]: s for s in strategies}
    strat_by_type: dict[str, list[dict]] = defaultdict(list)
    for s in strategies:
        strat_by_type[s["strategy_type"]].append(s)

    paper_trades = get_terminal_paper_trades(conn)
    crypto_positions = get_strategy_positions(conn, CRYPTO_STRATEGY_ID)

    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "changes_made": [],
        "warnings": [],
        "circuit_breakers": {},
        "btc_regime": {},
        "wallet_regimes": {},
        "data_gaps": {},
    }

    # -------------------------------------------------------------------------
    # Layer 1: Circuit Breakers
    # -------------------------------------------------------------------------

    # 1a. Weekly drawdown
    drawdown_info = compute_weekly_drawdown(paper_trades)
    report["circuit_breakers"]["portfolio_drawdown"] = drawdown_info
    if drawdown_info["drawdown"] >= DRAWDOWN_THRESHOLD_USD:
        msg = (
            f"Portfolio drawdown ${drawdown_info['drawdown']:.2f} exceeds "
            f"${DRAWDOWN_THRESHOLD_USD} weekly threshold "
            f"(weekly PnL: ${drawdown_info['weekly_pnl']:.2f})"
        )
        report["warnings"].append({"type": "portfolio_drawdown", "message": msg})
        logger.warning("CIRCUIT BREAKER: %s", msg)

    # 1b. Consecutive paper_trader losses
    pt_outcomes = outcomes_from_trades(paper_trades)
    pt_streak = tail_loss_streak(pt_outcomes)
    report["circuit_breakers"]["paper_trader"] = {
        "total_trades": len(paper_trades),
        "win_rate": round(pt_outcomes.count("W") / len(pt_outcomes), 3) if pt_outcomes else None,
        "consecutive_loss_streak": pt_streak,
        "last_10": pt_outcomes[-10:],
    }
    if pt_streak >= PAPER_LOSS_STREAK_THRESHOLD:
        msg = f"Paper trader: {pt_streak} consecutive losses (threshold: {PAPER_LOSS_STREAK_THRESHOLD})"
        report["warnings"].append({"type": "paper_streak", "message": msg})
        logger.warning("CIRCUIT BREAKER: %s", msg)

    # 1c. Consecutive crypto_momentum losses
    crypto_outcomes = outcomes_from_trades(crypto_positions)
    crypto_streak = tail_loss_streak(crypto_outcomes)
    report["circuit_breakers"]["crypto_momentum"] = {
        "strategy_id": CRYPTO_STRATEGY_ID,
        "total_positions": len(crypto_positions),
        "win_rate": round(crypto_outcomes.count("W") / len(crypto_outcomes), 3) if crypto_outcomes else None,
        "consecutive_loss_streak": crypto_streak,
        "last_10": crypto_outcomes[-10:],
    }

    crypto_strat = strat_by_id.get(CRYPTO_STRATEGY_ID)
    crypto_streak_triggered = crypto_streak >= CRYPTO_LOSS_STREAK_THRESHOLD

    if crypto_streak_triggered and crypto_strat and crypto_strat["status"] == "active":
        reason = f"{crypto_streak} consecutive losses (threshold: {CRYPTO_LOSS_STREAK_THRESHOLD}, actual WR: {crypto_outcomes.count('W')}/{len(crypto_outcomes)})"
        if not status_only:
            set_strategy_status(conn, CRYPTO_STRATEGY_ID, "paused", f"circuit_breaker:{reason}", dry_run)
        tg_msg = f"⏸ Paused crypto_momentum_v1 (id={CRYPTO_STRATEGY_ID}): streak circuit breaker — {reason}"
        report["changes_made"].append({"action": "pause", "strategy_id": CRYPTO_STRATEGY_ID, "reason": reason, "trigger": "circuit_breaker"})
        if not (status_only or dry_run):
            send_telegram(tg_msg)

    # -------------------------------------------------------------------------
    # Layer 2: BTC Volatility Regime (crypto_momentum_v1 only)
    # -------------------------------------------------------------------------

    btc = get_btc_regime()
    report["btc_regime"] = btc

    if btc["error"]:
        logger.warning("BTC regime fetch failed: %s", btc["error"])
    else:
        current_crypto_status = strat_by_id.get(CRYPTO_STRATEGY_ID, {}).get("status", "unknown")
        # Re-check after potential streak pause
        for s in strategies:
            if s["id"] == CRYPTO_STRATEGY_ID:
                if any(c["strategy_id"] == CRYPTO_STRATEGY_ID for c in report["changes_made"]):
                    current_crypto_status = "paused"
                    break

        if btc["regime"] == "choppy" and current_crypto_status == "active" and not crypto_streak_triggered:
            reason = (
                f"BTC choppy — 24h change {btc['change_24h_pct']:+.1f}%, "
                f"realized vol {btc['realized_vol_pct']:.2f}%/4h bar"
            )
            if not status_only:
                set_strategy_status(conn, CRYPTO_STRATEGY_ID, "paused", f"btc_regime:{reason}", dry_run)
            tg_msg = f"⏸ Paused crypto_momentum_v1 (id={CRYPTO_STRATEGY_ID}): {reason}"
            report["changes_made"].append({"action": "pause", "strategy_id": CRYPTO_STRATEGY_ID, "reason": reason, "trigger": "btc_regime"})
            if not (status_only or dry_run):
                send_telegram(tg_msg)

        elif btc["regime"] == "trending" and current_crypto_status == "paused":
            # Only auto-resume if we regime-paused it (not manually paused)
            crypto_strat_fresh = strat_by_id.get(CRYPTO_STRATEGY_ID, {})
            if is_regime_paused(crypto_strat_fresh):
                reason = (
                    f"BTC trending — 24h change {btc['change_24h_pct']:+.1f}%, "
                    f"realized vol {btc['realized_vol_pct']:.2f}%/4h bar"
                )
                if not status_only:
                    set_strategy_status(conn, CRYPTO_STRATEGY_ID, "active", f"btc_regime_resume:{reason}", dry_run)
                tg_msg = f"▶️ Resumed crypto_momentum_v1 (id={CRYPTO_STRATEGY_ID}): {reason}"
                report["changes_made"].append({"action": "resume", "strategy_id": CRYPTO_STRATEGY_ID, "reason": reason, "trigger": "btc_regime"})
                if not (status_only or dry_run):
                    send_telegram(tg_msg)

    # -------------------------------------------------------------------------
    # Layer 3: Per-Wallet Degradation (insider_copy_v1)
    # -------------------------------------------------------------------------

    wallet_regimes = get_wallet_regimes(paper_trades)
    report["wallet_regimes"] = wallet_regimes

    # Check S.T.F. strategy (id=7) wallet specifically
    stf_strat = strat_by_id.get(STF_STRATEGY_ID)
    if stf_strat:
        stf_wallet = stf_strat.get("wallet", "")
        stf_wallet_regime = wallet_regimes.get(stf_wallet)

        if stf_wallet_regime:
            report["circuit_breakers"]["stf_wallet"] = {
                "wallet": stf_wallet[:20] + "...",
                "regime": stf_wallet_regime,
            }
            if stf_wallet_regime["status"] in ("degrading", "cold") and stf_strat["status"] == "active":
                reason = (
                    f"wallet {stf_wallet[:16]}... degrading — "
                    f"last8_wr={stf_wallet_regime.get('last_8_wr', 0):.0%}, "
                    f"streak={stf_wallet_regime.get('streak', 0)}"
                )
                if stf_wallet_regime.get("degradation_reason"):
                    reason += f" ({stf_wallet_regime['degradation_reason']})"
                if not status_only:
                    set_strategy_status(conn, STF_STRATEGY_ID, "paused", f"wallet_degradation:{reason}", dry_run)
                tg_msg = f"⏸ Paused S.T.F. insider_copy_v1 (id={STF_STRATEGY_ID}): {reason}"
                report["changes_made"].append({"action": "pause", "strategy_id": STF_STRATEGY_ID, "reason": reason, "trigger": "wallet_degradation"})
                if not (status_only or dry_run):
                    send_telegram(tg_msg)

            elif stf_wallet_regime["status"] in ("hot", "moderate") and stf_strat["status"] == "paused":
                if is_regime_paused(stf_strat):
                    reason = (
                        f"wallet {stf_wallet[:16]}... recovered — "
                        f"last8_wr={stf_wallet_regime.get('last_8_wr', 0):.0%}"
                    )
                    if not status_only:
                        set_strategy_status(conn, STF_STRATEGY_ID, "active", f"wallet_recovery:{reason}", dry_run)
                    tg_msg = f"▶️ Resumed S.T.F. insider_copy_v1 (id={STF_STRATEGY_ID}): {reason}"
                    report["changes_made"].append({"action": "resume", "strategy_id": STF_STRATEGY_ID, "reason": reason, "trigger": "wallet_recovery"})
                    if not (status_only or dry_run):
                        send_telegram(tg_msg)
        else:
            report["circuit_breakers"]["stf_wallet"] = {
                "wallet": (stf_wallet[:20] + "...") if stf_wallet else "no wallet configured",
                "regime": "insufficient_data",
                "note": "S.T.F. wallet has no closed paper_trades yet (operates via strategy_positions)",
            }

    # -------------------------------------------------------------------------
    # Layer 4: Data gaps — strategies without enough resolved trades
    # -------------------------------------------------------------------------

    # Count closed positions per strategy
    all_sp = _fetch_all(conn, "SELECT strategy_id, state, pnl_usdc FROM strategy_positions WHERE state='closed'")
    sp_counts: dict[str, int] = defaultdict(int)
    for p in all_sp:
        sp_counts[str(p["strategy_id"])] += 1

    data_gap_types = {
        "political_no_value_v1", "term_structure_v1", "news_intel_v1",
        "cross_arb_v1", "sports_no_value_v1", "sports_line_dislocation_v1",
    }
    for s in strategies:
        if s["strategy_type"] in data_gap_types:
            count = sp_counts.get(str(s["id"]), 0)
            report["data_gaps"][s["id"]] = {
                "label": s["label"],
                "strategy_type": s["strategy_type"],
                "status": s["status"],
                "closed_positions": count,
                "evaluation": "active" if count >= MIN_TRADES_FOR_EVALUATION else
                              f"insufficient_data: {count} closed positions, need {MIN_TRADES_FOR_EVALUATION} to evaluate regime",
            }

    conn.close()
    return report


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_status(report: dict) -> str:
    lines = [
        f"=== REGIME MONITOR — {report['timestamp'][:16]} UTC ===",
        "",
    ]

    # Circuit breakers
    lines.append("CIRCUIT BREAKERS:")

    dd = report["circuit_breakers"].get("portfolio_drawdown", {})
    lines.append(
        f"  Portfolio (this week): {dd.get('trade_count', 0)} closed, "
        f"PnL=${dd.get('weekly_pnl', 0):.2f}, "
        f"drawdown=${dd.get('drawdown', 0):.2f} (threshold: ${DRAWDOWN_THRESHOLD_USD:.0f})"
    )

    pt = report["circuit_breakers"].get("paper_trader", {})
    if pt:
        wr = f"{pt.get('win_rate', 0)*100:.1f}%" if pt.get("win_rate") is not None else "n/a"
        lines.append(
            f"  Paper trader: {pt.get('total_trades', 0)} closed, WR={wr}, "
            f"streak={pt.get('consecutive_loss_streak', 0)} (threshold: {PAPER_LOSS_STREAK_THRESHOLD}), "
            f"last10={pt.get('last_10', [])}"
        )

    cm = report["circuit_breakers"].get("crypto_momentum", {})
    if cm:
        wr = f"{cm.get('win_rate', 0)*100:.1f}%" if cm.get("win_rate") is not None else "n/a"
        lines.append(
            f"  Crypto momentum (id={CRYPTO_STRATEGY_ID}): {cm.get('total_positions', 0)} closed, WR={wr}, "
            f"streak={cm.get('consecutive_loss_streak', 0)} (threshold: {CRYPTO_LOSS_STREAK_THRESHOLD}), "
            f"last10={cm.get('last_10', [])}"
        )

    stf_w = report["circuit_breakers"].get("stf_wallet", {})
    if stf_w:
        wr_info = stf_w.get("regime", {})
        if isinstance(wr_info, dict) and wr_info.get("status") != "insufficient_data":
            lines.append(
                f"  S.T.F. wallet ({stf_w.get('wallet', '')[:20]}): "
                f"status={wr_info.get('status')}, "
                f"last8_wr={wr_info.get('last_8_wr', 0)*100:.0f}%, "
                f"streak={wr_info.get('streak', 0)}"
            )
        else:
            note = wr_info.get("note", "insufficient_data") if isinstance(wr_info, dict) else str(wr_info)
            lines.append(f"  S.T.F. wallet ({stf_w.get('wallet', '')[:20]}): {note}")

    lines.append("")

    # BTC regime
    btc = report.get("btc_regime", {})
    if btc.get("error"):
        lines.append(f"BTC REGIME: error — {btc['error']}")
    else:
        lines.append(
            f"BTC REGIME: {btc.get('regime', 'unknown').upper()} — "
            f"price=${btc.get('price', 0):,.0f}, "
            f"24h={btc.get('change_24h_pct', 0):+.2f}%, "
            f"realized_vol={btc.get('realized_vol_pct', 0):.3f}%/4h"
        )
    lines.append("")

    # Wallet regimes (only those with ≥ WALLET_MIN_TRADES)
    wallet_regimes = report.get("wallet_regimes", {})
    evaluable = {w: v for w, v in wallet_regimes.items() if v.get("status") != "insufficient_data"}
    if evaluable:
        lines.append(f"WALLET REGIMES (≥{WALLET_MIN_TRADES} trades):")
        for wallet, info in evaluable.items():
            lines.append(
                f"  {wallet[:20]}...: {info.get('status').upper()}, "
                f"total={info.get('total_trades')}, WR={info.get('total_wr', 0)*100:.0f}%, "
                f"last8_wr={info.get('last_8_wr', 0)*100:.0f}%, streak={info.get('streak', 0)}"
            )
    else:
        lines.append(f"WALLET REGIMES: no wallets with ≥{WALLET_MIN_TRADES} closed trades in paper_trades")
    lines.append("")

    # Data gaps
    gaps = report.get("data_gaps", {})
    if gaps:
        lines.append("DATA GAPS (strategies without enough resolved trades):")
        for sid, info in gaps.items():
            lines.append(
                f"  id={sid} {info.get('strategy_type')}: {info.get('evaluation')}"
            )
    lines.append("")

    # Warnings
    warnings = report.get("warnings", [])
    if warnings:
        lines.append("WARNINGS:")
        for w in warnings:
            lines.append(f"  [{w['type'].upper()}] {w['message']}")
        lines.append("")

    # Changes
    changes = report.get("changes_made", [])
    if changes:
        lines.append("CHANGES MADE:")
        for c in changes:
            lines.append(f"  {c['action'].upper()} id={c['strategy_id']} via {c['trigger']}: {c['reason']}")
    else:
        lines.append("CHANGES MADE: none")

    if report.get("dry_run"):
        lines.append("")
        lines.append("[DRY-RUN — no DB changes written]")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Regime monitor — capital protection via grounded signals")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate but make no DB changes")
    parser.add_argument("--status", action="store_true", help="Show current signals, no changes")
    parser.add_argument("--reset", type=int, metavar="ID", help="Clear regime-pause on strategy ID")
    args = parser.parse_args()

    if args.reset:
        conn = _get_conn()
        strats = get_strategies(conn)
        target = next((s for s in strats if s["id"] == args.reset), None)
        if not target:
            print(f"Strategy {args.reset} not found")
            sys.exit(1)
        if not is_regime_paused(target):
            print(f"Strategy {args.reset} was not regime-paused (current status: {target['status']})")
            print("Use direct DB update to override manual pauses")
            sys.exit(0)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE strategies SET status='active', ai_analysis=?, updated_at=? WHERE id=?",
            (f"[regime_reset:{now_iso}] manual reset via regime_monitor --reset", now_iso, args.reset)
        )
        conn.commit()
        conn.close()
        print(f"Strategy {args.reset} regime-pause cleared → active")
        return

    report = evaluate(dry_run=args.dry_run, status_only=args.status)
    print(format_status(report))

    if not (args.status or args.dry_run):
        try:
            write_brain(report)
        except Exception as e:
            logger.error("Brain write failed: %s", e)


if __name__ == "__main__":
    main()

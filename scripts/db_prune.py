#!/usr/bin/env python3
"""
Database pruning maintenance script.

Removes stale/garbage data that accumulates over time:
- signal_history: low_signal rows older than retention period
- score_history: keeps only latest N scores per low-value wallet
- analysis_runs: keeps only latest N runs per wallet
- odds_snapshots: removes data for dropped sports older than retention
- edge_observations: removes old observations for dropped sports

Safe to run while services are active (uses transactions, no schema changes).

Usage:
    python3 scripts/db_prune.py                    # dry-run (default)
    python3 scripts/db_prune.py --execute          # actually delete
    python3 scripts/db_prune.py --execute --quiet  # cron-friendly
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / os.getenv("DB_PATH", "sniper_tracker.db")

# --- Configuration ---

# strategy_positions: keep closed positions for this many days
# Open/pending positions are never pruned.
STRATEGY_POSITIONS_CLOSED_KEEP_DAYS = 90

# strategy_executions: keep records for this many days
STRATEGY_EXECUTIONS_KEEP_DAYS = 90

# strategy_runs: keep run records for this many days
STRATEGY_RUNS_KEEP_DAYS = 14

# signal_history: delete position_low_signal older than this many days
# Low-signal wallets are noise; 2 days is enough to avoid re-entering same position
SIGNAL_HISTORY_LOW_SIGNAL_DAYS = 2

# signal_history: delete position_speculator_ladder older than this many days
SIGNAL_HISTORY_SPEC_LADDER_DAYS = 5

# score_history: for low_signal/speculator_bot wallets, keep only latest N per wallet
SCORE_HISTORY_KEEP_PER_WALLET = 3

# score_history: for high_winrate_review wallets keep more (trend analysis) but still bounded
# ~5 per day × 30 = ~6 days of trend data — enough to detect degradation patterns
SCORE_HISTORY_HIGH_WINRATE_KEEP = 30

# analysis_runs: keep only latest N runs per wallet
ANALYSIS_RUNS_KEEP_PER_WALLET = 5

# odds_snapshots: sports no longer in strategy — delete immediately (0 days)
# These sports will never be traded; no reason to retain their odds data
ODDS_DROPPED_SPORTS_DAYS = 0
DROPPED_SPORTS = [
    "icehockey_nhl",
    "baseball_mlb",
    "americanfootball_nfl",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_uefa_champs_league",
    "mma_mixed_martial_arts",
]

# edge_observations: prune old observations for dropped sports
EDGE_DROPPED_SPORTS_DAYS = 0

# odds_snapshots: active sports — recent_odds_events only needs rows for upcoming events
# Keep 2 days of history (enough for line movement detection); older data is dead weight
ODDS_ACTIVE_KEEP_DAYS = 2

# followed_wallet_activity: keep only recent activity
FOLLOWED_ACTIVITY_DAYS = 14

# low_buy_trades: raw ingested trades — delete rows older than this many days.
# These are the raw feed entries used to build candidate_users; once a wallet
# has been analyzed the raw rows serve no ongoing purpose.
LOW_BUY_TRADES_KEEP_DAYS = 14

# candidate_users: wallet candidate list fed by low_buy_trades stream.
# Entries not seen in X days are dead candidates — the wallet stopped trading
# the pattern we care about.
CANDIDATE_USERS_STALE_DAYS = 7

# user_analysis: hard-purge confirmed bots and wallets with zero trades.
# Poor performers (win < threshold, n >= min_sample) are also removed.
PURGE_CONFIRMED_BOTS = True
PURGE_ZERO_TRADE_WALLETS = True
PURGE_POOR_PERFORMERS = True
POOR_PERFORMER_MAX_WIN_RATE = 0.35   # below this is "poor"
POOR_PERFORMER_MIN_SAMPLE = 5        # require at least this many closed trades

# trade_ideas: intel table cleanup
# - expired/acted/resolved ideas older than this many hours past their expiry are deleted
TRADE_IDEAS_STALE_AFTER_HOURS = 48
# - news_items: keep processed items for this many days; unprocessed for this many days
NEWS_ITEMS_PROCESSED_KEEP_DAYS = 3
NEWS_ITEMS_UNPROCESSED_KEEP_DAYS = 7

# poly_price_snapshots: price feed for strategies — strategies only read last 4h.
# Keep 30 days for any future backtesting needs; anything older is dead weight.
# At ~90K rows/day this table grows to millions of rows quickly without pruning.
POLY_PRICE_SNAPSHOTS_KEEP_DAYS = 30


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _prune_signal_history(conn, dry_run=True):
    """Delete low-value signal_history rows older than retention."""
    cutoff_low = f"-{SIGNAL_HISTORY_LOW_SIGNAL_DAYS} days"
    cutoff_spec = f"-{SIGNAL_HISTORY_SPEC_LADDER_DAYS} days"

    count_low = conn.execute(
        """SELECT COUNT(*) as c FROM signal_history
           WHERE position_classification = 'position_low_signal'
           AND captured_at < datetime('now', ?)""",
        (cutoff_low,),
    ).fetchone()[0]

    count_spec = conn.execute(
        """SELECT COUNT(*) as c FROM signal_history
           WHERE position_classification = 'position_speculator_ladder'
           AND captured_at < datetime('now', ?)""",
        (cutoff_spec,),
    ).fetchone()[0]

    total = count_low + count_spec
    if not dry_run and total > 0:
        conn.execute(
            """DELETE FROM signal_history
               WHERE position_classification = 'position_low_signal'
               AND captured_at < datetime('now', ?)""",
            (cutoff_low,),
        )
        conn.execute(
            """DELETE FROM signal_history
               WHERE position_classification = 'position_speculator_ladder'
               AND captured_at < datetime('now', ?)""",
            (cutoff_spec,),
        )
    return total


def _prune_score_history(conn, dry_run=True):
    """Keep only latest N scores per wallet for low-value classifications.

    Retention tiers:
      - low_signal, speculator_bot  → keep 3  (noise; score won't improve meaningfully)
      - high_winrate_review          → keep 30 (enough for ~6d trend; degrades fast otherwise)
      - All other classifications    → unlimited (elite/insider wallets — keep full history)
    """
    total = 0

    # Tier 1: low-value wallets — keep only 3
    rows = conn.execute(
        """SELECT wallet, COUNT(*) as c FROM score_history
           WHERE classification IN ('low_signal', 'speculator_bot')
           GROUP BY wallet
           HAVING c > ?""",
        (SCORE_HISTORY_KEEP_PER_WALLET,),
    ).fetchall()
    for row in rows:
        wallet, count = row
        excess = count - SCORE_HISTORY_KEEP_PER_WALLET
        total += excess
        if not dry_run:
            conn.execute(
                """DELETE FROM score_history WHERE id IN (
                       SELECT id FROM score_history
                       WHERE wallet = ? AND classification IN ('low_signal', 'speculator_bot')
                       ORDER BY analyzed_ts DESC
                       LIMIT -1 OFFSET ?
                   )""",
                (wallet, SCORE_HISTORY_KEEP_PER_WALLET),
            )

    # Tier 2: high_winrate_review wallets — keep 30 (trend window)
    rows2 = conn.execute(
        """SELECT wallet, COUNT(*) as c FROM score_history
           WHERE classification = 'high_winrate_review'
           GROUP BY wallet
           HAVING c > ?""",
        (SCORE_HISTORY_HIGH_WINRATE_KEEP,),
    ).fetchall()
    for row in rows2:
        wallet, count = row
        excess = count - SCORE_HISTORY_HIGH_WINRATE_KEEP
        total += excess
        if not dry_run:
            conn.execute(
                """DELETE FROM score_history WHERE id IN (
                       SELECT id FROM score_history
                       WHERE wallet = ? AND classification = 'high_winrate_review'
                       ORDER BY analyzed_ts DESC
                       LIMIT -1 OFFSET ?
                   )""",
                (wallet, SCORE_HISTORY_HIGH_WINRATE_KEEP),
            )

    return total


def _prune_analysis_runs(conn, dry_run=True):
    """Keep only latest N analysis runs per wallet."""
    rows = conn.execute(
        """SELECT wallet, COUNT(*) as c FROM analysis_runs
           GROUP BY wallet
           HAVING c > ?""",
        (ANALYSIS_RUNS_KEEP_PER_WALLET,),
    ).fetchall()

    total = 0
    for row in rows:
        wallet = row[0]
        excess = row[1] - ANALYSIS_RUNS_KEEP_PER_WALLET
        total += excess
        if not dry_run:
            conn.execute(
                """DELETE FROM analysis_runs WHERE rowid IN (
                       SELECT rowid FROM analysis_runs
                       WHERE wallet = ?
                       ORDER BY started_ts DESC
                       LIMIT -1 OFFSET ?
                   )""",
                (wallet, ANALYSIS_RUNS_KEEP_PER_WALLET),
            )
    return total


def _prune_odds_snapshots(conn, dry_run=True):
    """Remove odds data for sports no longer in the strategy."""
    if not DROPPED_SPORTS:
        return 0
    placeholders = ",".join("?" for _ in DROPPED_SPORTS)
    if ODDS_DROPPED_SPORTS_DAYS <= 0:
        # Delete ALL data for dropped sports immediately
        count = conn.execute(
            f"SELECT COUNT(*) as c FROM odds_snapshots WHERE sport_key IN ({placeholders})",
            DROPPED_SPORTS,
        ).fetchone()[0]
        if not dry_run and count > 0:
            conn.execute(
                f"DELETE FROM odds_snapshots WHERE sport_key IN ({placeholders})",
                DROPPED_SPORTS,
            )
    else:
        cutoff = f"-{ODDS_DROPPED_SPORTS_DAYS} days"
        count = conn.execute(
            f"""SELECT COUNT(*) as c FROM odds_snapshots
                WHERE sport_key IN ({placeholders})
                AND captured_at < datetime('now', ?)""",
            DROPPED_SPORTS + [cutoff],
        ).fetchone()[0]
        if not dry_run and count > 0:
            conn.execute(
                f"""DELETE FROM odds_snapshots
                    WHERE sport_key IN ({placeholders})
                    AND captured_at < datetime('now', ?)""",
                DROPPED_SPORTS + [cutoff],
            )
    return count


def _prune_odds_active_sports(conn, dry_run=True):
    """Remove old odds snapshots for active sports.

    recent_odds_events() only queries commence_time within the next 72h, so
    historical snapshots older than ODDS_ACTIVE_KEEP_DAYS are never read.
    """
    cutoff = f"-{ODDS_ACTIVE_KEEP_DAYS} days"
    # Exclude dropped sports (handled separately with 0-day retention)
    if DROPPED_SPORTS:
        placeholders = ",".join("?" for _ in DROPPED_SPORTS)
        count = conn.execute(
            f"""SELECT COUNT(*) FROM odds_snapshots
                WHERE sport_key NOT IN ({placeholders})
                AND captured_at < datetime('now', ?)""",
            DROPPED_SPORTS + [cutoff],
        ).fetchone()[0]
        if not dry_run and count > 0:
            conn.execute(
                f"""DELETE FROM odds_snapshots
                    WHERE sport_key NOT IN ({placeholders})
                    AND captured_at < datetime('now', ?)""",
                DROPPED_SPORTS + [cutoff],
            )
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM odds_snapshots WHERE captured_at < datetime('now', ?)",
            (cutoff,),
        ).fetchone()[0]
        if not dry_run and count > 0:
            conn.execute(
                "DELETE FROM odds_snapshots WHERE captured_at < datetime('now', ?)",
                (cutoff,),
            )
    return count


def _prune_edge_observations(conn, dry_run=True):
    """Remove edge observations for dropped sports."""
    if not DROPPED_SPORTS:
        return 0
    placeholders = ",".join("?" for _ in DROPPED_SPORTS)
    if EDGE_DROPPED_SPORTS_DAYS <= 0:
        count = conn.execute(
            f"SELECT COUNT(*) as c FROM edge_observations WHERE sport_key IN ({placeholders})",
            DROPPED_SPORTS,
        ).fetchone()[0]
        if not dry_run and count > 0:
            conn.execute(
                f"DELETE FROM edge_observations WHERE sport_key IN ({placeholders})",
                DROPPED_SPORTS,
            )
    else:
        cutoff = f"-{EDGE_DROPPED_SPORTS_DAYS} days"
        count = conn.execute(
            f"""SELECT COUNT(*) as c FROM edge_observations
                WHERE sport_key IN ({placeholders})
                AND observed_at < datetime('now', ?)""",
            DROPPED_SPORTS + [cutoff],
        ).fetchone()[0]
        if not dry_run and count > 0:
            conn.execute(
                f"""DELETE FROM edge_observations
                    WHERE sport_key IN ({placeholders})
                    AND observed_at < datetime('now', ?)""",
                DROPPED_SPORTS + [cutoff],
            )
    return count


def _prune_followed_wallet_activity(conn, dry_run=True):
    """Remove old followed_wallet_activity beyond retention period."""
    cutoff_ts = int(time.time()) - (FOLLOWED_ACTIVITY_DAYS * 86400)
    count = conn.execute(
        "SELECT COUNT(*) as c FROM followed_wallet_activity WHERE activity_ts < ?",
        (cutoff_ts,),
    ).fetchone()[0]
    if not dry_run and count > 0:
        conn.execute(
            "DELETE FROM followed_wallet_activity WHERE activity_ts < ?",
            (cutoff_ts,),
        )
    return count


def _prune_strategy_positions(conn, dry_run=True):
    """Remove closed strategy_positions beyond retention period.

    Only deletes state='closed' rows — open/pending_entry/pending_exit are never touched.
    """
    cutoff_ts = int(time.time()) - (STRATEGY_POSITIONS_CLOSED_KEEP_DAYS * 86400)
    count = conn.execute(
        "SELECT COUNT(*) FROM strategy_positions WHERE state='closed' AND closed_ts < ? AND closed_ts > 0",
        (cutoff_ts,),
    ).fetchone()[0]
    if not dry_run and count > 0:
        conn.execute(
            "DELETE FROM strategy_positions WHERE state='closed' AND closed_ts < ? AND closed_ts > 0",
            (cutoff_ts,),
        )
    return count


def _prune_strategy_executions(conn, dry_run=True):
    """Remove old strategy_executions log entries."""
    cutoff_ts = int(time.time()) - (STRATEGY_EXECUTIONS_KEEP_DAYS * 86400)
    count = conn.execute(
        "SELECT COUNT(*) FROM strategy_executions WHERE created_at < datetime(?, 'unixepoch')",
        (cutoff_ts,),
    ).fetchone()[0]
    if not dry_run and count > 0:
        conn.execute(
            "DELETE FROM strategy_executions WHERE created_at < datetime(?, 'unixepoch')",
            (cutoff_ts,),
        )
    return count


def _prune_strategy_runs(conn, dry_run=True):
    """Remove old strategy_runs log entries (per-cycle run metadata)."""
    cutoff_ts = int(time.time()) - (STRATEGY_RUNS_KEEP_DAYS * 86400)
    count = conn.execute(
        "SELECT COUNT(*) FROM strategy_runs WHERE started_at < datetime(?, 'unixepoch')",
        (cutoff_ts,),
    ).fetchone()[0]
    if not dry_run and count > 0:
        conn.execute(
            "DELETE FROM strategy_runs WHERE started_at < datetime(?, 'unixepoch')",
            (cutoff_ts,),
        )
    return count


def _prune_low_buy_trades(conn, dry_run=True):
    """Delete low_buy_trades rows older than LOW_BUY_TRADES_KEEP_DAYS.

    These are raw ingest rows used only to populate candidate_users.  Once
    processed they have no ongoing value and accumulate indefinitely.
    """
    cutoff_epoch = int((datetime.now(timezone.utc) - timedelta(days=LOW_BUY_TRADES_KEEP_DAYS)).timestamp())
    count = conn.execute(
        "SELECT COUNT(*) FROM low_buy_trades WHERE timestamp < ?",
        (cutoff_epoch,),
    ).fetchone()[0]
    if not dry_run and count > 0:
        conn.execute(
            "DELETE FROM low_buy_trades WHERE timestamp < ?",
            (cutoff_epoch,),
        )
    return count


def _prune_candidate_users(conn, dry_run=True):
    """Delete candidate_users rows not seen in CANDIDATE_USERS_STALE_DAYS days.

    A stale candidate has stopped generating low-buy signals; keeping them
    pollutes the analysis queue and inflates wallet counts.
    """
    cutoff = f"-{CANDIDATE_USERS_STALE_DAYS} days"
    count = conn.execute(
        "SELECT COUNT(*) FROM candidate_users WHERE last_seen_ts < datetime('now', ?)",
        (cutoff,),
    ).fetchone()[0]
    if not dry_run and count > 0:
        conn.execute(
            "DELETE FROM candidate_users WHERE last_seen_ts < datetime('now', ?)",
            (cutoff,),
        )
    return count


def _purge_user_analysis(conn, dry_run=True):
    """Hard-purge low-value rows from user_analysis:

    1. Confirmed bots (is_bot = 1)
    2. Zero-trade wallets (trade_count = 0)
    3. Poor performers (win_rate < threshold AND closed_sample_size >= min_sample)

    These wallets will never be followed or copied.  Removing them reduces
    query overhead and keeps wallet counts meaningful.
    """
    total = 0

    if PURGE_CONFIRMED_BOTS:
        count = conn.execute(
            "SELECT COUNT(*) FROM user_analysis WHERE is_bot = 1"
        ).fetchone()[0]
        total += count
        if not dry_run and count > 0:
            conn.execute("DELETE FROM user_analysis WHERE is_bot = 1")

    if PURGE_ZERO_TRADE_WALLETS:
        count = conn.execute(
            "SELECT COUNT(*) FROM user_analysis WHERE trade_count = 0"
        ).fetchone()[0]
        total += count
        if not dry_run and count > 0:
            conn.execute("DELETE FROM user_analysis WHERE trade_count = 0")

    if PURGE_POOR_PERFORMERS:
        count = conn.execute(
            """SELECT COUNT(*) FROM user_analysis
               WHERE closed_sample_size >= ?
                 AND win_rate < ?
                 AND (is_bot IS NULL OR is_bot = 0)""",
            (POOR_PERFORMER_MIN_SAMPLE, POOR_PERFORMER_MAX_WIN_RATE),
        ).fetchone()[0]
        total += count
        if not dry_run and count > 0:
            conn.execute(
                """DELETE FROM user_analysis
                   WHERE closed_sample_size >= ?
                     AND win_rate < ?
                     AND (is_bot IS NULL OR is_bot = 0)""",
                (POOR_PERFORMER_MIN_SAMPLE, POOR_PERFORMER_MAX_WIN_RATE),
            )

    return total


def _prune_trade_ideas(conn, dry_run=True):
    """Delete stale trade_ideas rows.

    Removes:
    - Non-open ideas (acted/resolved/expired) whose created_at is older than
      TRADE_IDEAS_STALE_AFTER_HOURS hours.
    - Open ideas whose expires_at has passed by more than TRADE_IDEAS_STALE_AFTER_HOURS
      hours (i.e. truly expired with no action taken).
    """
    cutoff = f"-{TRADE_IDEAS_STALE_AFTER_HOURS} hours"

    # Non-open terminal states: acted/resolved/expired
    count_terminal = conn.execute(
        """SELECT COUNT(*) FROM trade_ideas
           WHERE status != 'open'
             AND created_at < datetime('now', ?)""",
        (cutoff,),
    ).fetchone()[0]

    # Open but expired (expires_at set and in the past beyond grace period)
    count_expired_open = conn.execute(
        """SELECT COUNT(*) FROM trade_ideas
           WHERE status = 'open'
             AND expires_at IS NOT NULL
             AND expires_at < datetime('now', ?)""",
        (cutoff,),
    ).fetchone()[0]

    total = count_terminal + count_expired_open
    if not dry_run and total > 0:
        conn.execute(
            """DELETE FROM trade_ideas
               WHERE status != 'open'
                 AND created_at < datetime('now', ?)""",
            (cutoff,),
        )
        conn.execute(
            """DELETE FROM trade_ideas
               WHERE status = 'open'
                 AND expires_at IS NOT NULL
                 AND expires_at < datetime('now', ?)""",
            (cutoff,),
        )
    return total


def _prune_news_items(conn, dry_run=True):
    """Delete old news_items based on processed status.

    - processed=1 rows older than NEWS_ITEMS_PROCESSED_KEEP_DAYS
    - processed=0 rows older than NEWS_ITEMS_UNPROCESSED_KEEP_DAYS
      (stale unprocessed items will never be acted on; signal is stale)
    """
    cutoff_proc = f"-{NEWS_ITEMS_PROCESSED_KEEP_DAYS} days"
    cutoff_unproc = f"-{NEWS_ITEMS_UNPROCESSED_KEEP_DAYS} days"

    count_proc = conn.execute(
        """SELECT COUNT(*) FROM news_items
           WHERE processed = 1
             AND fetched_at < datetime('now', ?)""",
        (cutoff_proc,),
    ).fetchone()[0]

    count_unproc = conn.execute(
        """SELECT COUNT(*) FROM news_items
           WHERE processed = 0
             AND fetched_at < datetime('now', ?)""",
        (cutoff_unproc,),
    ).fetchone()[0]

    total = count_proc + count_unproc
    if not dry_run and total > 0:
        conn.execute(
            """DELETE FROM news_items
               WHERE processed = 1
                 AND fetched_at < datetime('now', ?)""",
            (cutoff_proc,),
        )
        conn.execute(
            """DELETE FROM news_items
               WHERE processed = 0
                 AND fetched_at < datetime('now', ?)""",
            (cutoff_unproc,),
        )
    return total


def _prune_poly_price_snapshots(conn, dry_run=True):
    """Delete poly_price_snapshots rows older than POLY_PRICE_SNAPSHOTS_KEEP_DAYS.

    Strategies only query rows from the last 4 hours (price momentum checks).
    Historical rows have zero ongoing value and this table grows ~90K rows/day.
    30-day retention keeps data available for ad-hoc backtesting while preventing
    unbounded growth.
    """
    cutoff = f"-{POLY_PRICE_SNAPSHOTS_KEEP_DAYS} days"
    count = conn.execute(
        "SELECT COUNT(*) FROM poly_price_snapshots WHERE captured_at < datetime('now', ?)",
        (cutoff,),
    ).fetchone()[0]
    if not dry_run and count > 0:
        conn.execute(
            "DELETE FROM poly_price_snapshots WHERE captured_at < datetime('now', ?)",
            (cutoff,),
        )
    return count


def main():
    parser = argparse.ArgumentParser(description="Prune stale data from sniper DB")
    parser.add_argument("--execute", action="store_true", help="Actually delete (default is dry-run)")
    parser.add_argument("--quiet", action="store_true", help="Only print summary")
    args = parser.parse_args()

    dry_run = not args.execute
    now = _now_str()

    if not DB_PATH.exists():
        print(f"[{now}] ERROR: DB not found at {DB_PATH}")
        return 1

    db_size_before = DB_PATH.stat().st_size / (1024 * 1024)

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")

    mode = "DRY-RUN" if dry_run else "EXECUTE"
    if not args.quiet:
        print(f"[{now}] db_prune {mode} — DB: {db_size_before:.0f}MB")
        print()

    results = {}

    pruners = [
        ("signal_history (low_signal/spec_ladder)", _prune_signal_history),
        ("score_history (low-value + high_winrate_review wallets)", _prune_score_history),
        ("analysis_runs (excess per wallet)", _prune_analysis_runs),
        ("odds_snapshots (dropped sports)", _prune_odds_snapshots),
        (f"odds_snapshots (active sports >{ODDS_ACTIVE_KEEP_DAYS}d)", _prune_odds_active_sports),
        ("edge_observations (dropped sports)", _prune_edge_observations),
        ("followed_wallet_activity (>14d)", _prune_followed_wallet_activity),
        (f"strategy_positions (closed >{STRATEGY_POSITIONS_CLOSED_KEEP_DAYS}d)", _prune_strategy_positions),
        (f"strategy_executions (>{STRATEGY_EXECUTIONS_KEEP_DAYS}d)", _prune_strategy_executions),
        (f"strategy_runs (>{STRATEGY_RUNS_KEEP_DAYS}d)", _prune_strategy_runs),
        (f"low_buy_trades (>{LOW_BUY_TRADES_KEEP_DAYS}d)", _prune_low_buy_trades),
        (f"candidate_users (stale >{CANDIDATE_USERS_STALE_DAYS}d)", _prune_candidate_users),
        ("user_analysis (bots + zero-trade + poor performers)", _purge_user_analysis),
        (f"trade_ideas (terminal/expired >{TRADE_IDEAS_STALE_AFTER_HOURS}h)", _prune_trade_ideas),
        (f"news_items (processed >{NEWS_ITEMS_PROCESSED_KEEP_DAYS}d / unprocessed >{NEWS_ITEMS_UNPROCESSED_KEEP_DAYS}d)", _prune_news_items),
        (f"poly_price_snapshots (>{POLY_PRICE_SNAPSHOTS_KEEP_DAYS}d)", _prune_poly_price_snapshots),
    ]

    total_pruned = 0
    for label, func in pruners:
        t0 = time.time()
        count = func(conn, dry_run=dry_run)
        elapsed = time.time() - t0
        results[label] = count
        total_pruned += count
        if not args.quiet:
            action = "would delete" if dry_run else "deleted"
            print(f"  {label}: {action} {count:,} rows ({elapsed:.1f}s)")

    if not dry_run:
        conn.commit()

    conn.close()

    db_size_after = DB_PATH.stat().st_size / (1024 * 1024)

    if not args.quiet:
        print()
        print(f"  Total: {total_pruned:,} rows {'to prune' if dry_run else 'pruned'}")
        if not dry_run:
            print(f"  DB: {db_size_before:.0f}MB → {db_size_after:.0f}MB")
            print(f"  Run VACUUM or wal_checkpoint to reclaim disk space.")
        else:
            print(f"\n  Run with --execute to apply.")

    # Summary line for cron logs
    if args.quiet and not dry_run:
        print(f"[{now}] pruned {total_pruned:,} rows | DB {db_size_before:.0f}MB → {db_size_after:.0f}MB")

    return 0


if __name__ == "__main__":
    sys.exit(main())

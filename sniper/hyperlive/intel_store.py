"""Persistence for HL trader intelligence + forward-follow — the store the live UI reads.

Tables (hl_intel.sqlite):
- hl_tracked_traders: the copyable watchlist + their phase-1 verdict (for the UI cohort panel)
- hl_trader_state:    latest position snapshot per tracked trader (diffed to detect new entries)
- hl_follow_positions: paper follows opened on tracked entries, with net-of-fee bps on close
- hl_screen_runs:     verdict tally per leaderboard screen (for the UI)

Append/upsert only; read-only for the web layer. Paper/research; never PnL claims about real money."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from sniper.hyperlive.altshort_universe import provider_coin


COPY_MATURE_CULL_MIN_CLOSES = 20
COPY_EARLY_CULL_MIN_CLOSES = 5
COPY_EARLY_CULL_MAX_MEAN_BPS = -25.0
SCHEMA_VERSION = "2"


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA journal_size_limit=16777216")  # trim the WAL after a checkpoint reset
    return conn


def connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open the live intelligence ledger without creating files, WALs, or write locks."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hl_tracked_traders (
            wallet TEXT PRIMARY KEY, label TEXT, verdict TEXT, archetype TEXT,
            taker_frac REAL, profit_factor REAL, win_rate REAL, n_closes INTEGER,
            month_roi REAL, realized REAL, archetype_pass INTEGER DEFAULT 0,
            added_ts_ms INTEGER, updated_ts_ms INTEGER,
            lifecycle_status TEXT DEFAULT 'active', lifecycle_reason TEXT,
            lifecycle_updated_ts_ms INTEGER, last_seen_ts_ms INTEGER, culled_ts_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS hl_trader_state (
            wallet TEXT PRIMARY KEY, positions_json TEXT NOT NULL, ts_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hl_follow_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL, coin TEXT NOT NULL, side TEXT, kind TEXT,
            detect_ts_ms INTEGER NOT NULL, entry_mark REAL,
            status TEXT NOT NULL DEFAULT 'open',
            exit_ts_ms INTEGER, exit_mark REAL, net_bps REAL, exit_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_follow_status ON hl_follow_positions(status, detect_ts_ms DESC);
        CREATE TABLE IF NOT EXISTS hl_screen_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
            leaderboard_n INTEGER, prefiltered_n INTEGER, screened_n INTEGER, verdicts_json TEXT
        );
        CREATE TABLE IF NOT EXISTS hl_screened_traders (
            wallet TEXT PRIMARY KEY, verdict TEXT, archetype TEXT, taker_frac REAL,
            profit_factor REAL, win_rate REAL, n_closes INTEGER, top_coin_frac REAL,
            single_fill_frac REAL, month_roi REAL, realized REAL, account_value REAL,
            reason TEXT, archetype_pass INTEGER DEFAULT 0, ts_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS hl_copy_targets (
            wallet TEXT PRIMARY KEY, active INTEGER DEFAULT 1, label TEXT, added_ts_ms INTEGER,
            copy_tier TEXT DEFAULT 'proven', reason TEXT, updated_ts_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS hl_copy_book (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, coin TEXT NOT NULL,
            side TEXT, entry_mark REAL, detect_ts_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', exit_ts_ms INTEGER, exit_mark REAL, pnl_bps REAL,
            copy_tier TEXT DEFAULT 'unknown'
        );
        CREATE INDEX IF NOT EXISTS idx_copy_status ON hl_copy_book(status, detect_ts_ms DESC);
        CREATE TABLE IF NOT EXISTS hl_meta (
            key TEXT PRIMARY KEY, value TEXT, updated_ts_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS hl_copy_trader_state (
            wallet TEXT PRIMARY KEY, positions_json TEXT NOT NULL, ts_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hl_copy_source_events (
            wallet TEXT NOT NULL, tid TEXT NOT NULL, coin TEXT NOT NULL,
            side TEXT, direction TEXT, px REAL, sz REAL, start_position REAL,
            oid TEXT, tx_hash TEXT, source_event_ts_ms INTEGER NOT NULL,
            received_ts_ms INTEGER NOT NULL, clock_domain TEXT NOT NULL DEFAULT 'corrected_utc',
            is_snapshot INTEGER NOT NULL DEFAULT 0, is_recovery INTEGER NOT NULL DEFAULT 0,
            connection_epoch INTEGER NOT NULL, persisted_ts_ms INTEGER NOT NULL,
            copy_id INTEGER, match_status TEXT NOT NULL, match_reason TEXT, matched_ts_ms INTEGER,
            PRIMARY KEY(wallet, tid)
        );
        CREATE INDEX IF NOT EXISTS idx_copy_source_match
            ON hl_copy_source_events(wallet, match_status, source_event_ts_ms);
        CREATE TABLE IF NOT EXISTS hl_copy_source_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_received_ts_ms INTEGER NOT NULL, end_received_ts_ms INTEGER NOT NULL,
            connection_epoch INTEGER NOT NULL, dropped_count INTEGER NOT NULL,
            reason TEXT NOT NULL, persisted_ts_ms INTEGER NOT NULL,
            UNIQUE(connection_epoch, start_received_ts_ms, end_received_ts_ms, reason)
        );
        CREATE TABLE IF NOT EXISTS hl_scorecard_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL, holdout INTEGER DEFAULT 0,
            realized_n INTEGER, realized_mean REAL, ci_lo REAL, ci_hi REAL, spans_zero INTEGER,
            total_bps REAL, open_n INTEGER, alpha_n INTEGER, alpha_mean REAL, alpha_ci_lo REAL,
            alpha_ci_hi REAL, alpha_spans_zero INTEGER, top1_share REAL, top5_share REAL
        );
        CREATE INDEX IF NOT EXISTS idx_scorecard_snap ON hl_scorecard_snapshots(holdout, ts_ms DESC);
        CREATE TABLE IF NOT EXISTS hl_real_intents (
            cloid TEXT PRIMARY KEY,
            deployment_id TEXT NOT NULL,
            copy_id INTEGER NOT NULL,
            leg TEXT NOT NULL CHECK(leg IN ('open','close')),
            coin TEXT NOT NULL, side TEXT NOT NULL,
            requested_usd REAL, requested_sz REAL,
            state TEXT NOT NULL DEFAULT 'intended',
            reason TEXT,
            created_ts_ms INTEGER NOT NULL, updated_ts_ms INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hl_real_intents_copy_leg ON hl_real_intents(copy_id, leg);
        CREATE INDEX IF NOT EXISTS idx_hl_real_intents_state ON hl_real_intents(state);
        CREATE TABLE IF NOT EXISTS hl_real_fills (
            cloid TEXT PRIMARY KEY, copy_id INTEGER NOT NULL, leg TEXT NOT NULL,
            fill_px REAL, fill_sz REAL, fee_usd REAL, filled_ts_ms INTEGER,
            realized_usd REAL, paper_twin_pnl_bps REAL, real_net_bps REAL, drift_bps REAL
        );
        CREATE TABLE IF NOT EXISTS hl_altshort_arms (
            arm_id TEXT PRIMARY KEY,
            activated_ts_ms INTEGER NOT NULL,
            code_sha TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            policy_hash TEXT NOT NULL,
            targets_json TEXT NOT NULL,
            taxonomy_version TEXT NOT NULL,
            benchmark_version TEXT NOT NULL,
            regime_version TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            created_ts_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hl_altshort_observations (
            copy_id INTEGER PRIMARY KEY,
            arm_id TEXT NOT NULL,
            target TEXT NOT NULL,
            coin TEXT NOT NULL,
            side TEXT NOT NULL,
            detect_ts_ms INTEGER NOT NULL,
            entry_mark REAL NOT NULL,
            regime_json TEXT NOT NULL,
            benchmark_entry_json TEXT NOT NULL,
            latest_mark REAL,
            latest_mark_ts_ms INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            exit_mark REAL,
            exit_ts_ms INTEGER,
            wallet_net_bps REAL,
            benchmark_exit_json TEXT,
            provenance_status TEXT NOT NULL DEFAULT 'valid',
            FOREIGN KEY(arm_id) REFERENCES hl_altshort_arms(arm_id)
        );
        CREATE INDEX IF NOT EXISTS idx_altshort_obs_arm_status
            ON hl_altshort_observations(arm_id, status, detect_ts_ms);
        CREATE TABLE IF NOT EXISTS hl_altshort_signal_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            arm_id TEXT NOT NULL,
            coin TEXT NOT NULL,
            decision_ts_ms INTEGER NOT NULL,
            decision_mark REAL NOT NULL,
            runup REAL NOT NULL,
            regime_json TEXT NOT NULL,
            benchmark_entry_json TEXT NOT NULL,
            gated_in INTEGER NOT NULL,
            status TEXT NOT NULL,
            entry_ts_ms INTEGER,
            entry_mark REAL,
            latest_mark REAL,
            latest_mark_ts_ms INTEGER,
            exit_ts_ms INTEGER,
            exit_mark REAL,
            exit_reason TEXT,
            gross_bps REAL,
            funding_bps REAL,
            funding_pnl_bps REAL,
            funding_provenance_status TEXT,
            net_bps REAL,
            benchmark_exit_json TEXT,
            provenance_status TEXT NOT NULL DEFAULT 'valid',
            UNIQUE(arm_id, coin, decision_ts_ms),
            FOREIGN KEY(arm_id) REFERENCES hl_altshort_arms(arm_id)
        );
        CREATE INDEX IF NOT EXISTS idx_altshort_signal_arm_status
            ON hl_altshort_signal_observations(arm_id, status, decision_ts_ms);
        CREATE TABLE IF NOT EXISTS hl_altshort_signal_regimes (
            arm_id TEXT NOT NULL,
            decision_ts_ms INTEGER NOT NULL,
            regime_json TEXT NOT NULL,
            PRIMARY KEY(arm_id, decision_ts_ms),
            FOREIGN KEY(arm_id) REFERENCES hl_altshort_arms(arm_id)
        );
        CREATE INDEX IF NOT EXISTS idx_altshort_signal_regimes_arm_ts
            ON hl_altshort_signal_regimes(arm_id, decision_ts_ms);
        CREATE TABLE IF NOT EXISTS hl_altshort_regime_marks (
            ts_ms INTEGER NOT NULL,
            coin TEXT NOT NULL,
            mark REAL NOT NULL,
            open_mark REAL,
            high_mark REAL,
            low_mark REAL,
            funding_rate REAL,
            PRIMARY KEY(ts_ms, coin)
        );
        CREATE INDEX IF NOT EXISTS idx_altshort_regime_coin_ts
            ON hl_altshort_regime_marks(coin, ts_ms);
    """)
    conn.commit()
    row = conn.execute(
        "SELECT value FROM hl_meta WHERE key='intel_schema_version'"
    ).fetchone()
    if row and row["value"] == SCHEMA_VERSION:
        return
    _migrate(conn)
    conn.execute(
        "INSERT INTO hl_meta(key, value, updated_ts_ms) VALUES "
        "('intel_schema_version', ?, 0) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def save_scorecard_snapshot(conn, payload: dict, *, ts_ms: int, holdout: bool = False) -> int:
    """Persist the headline metrics of a build_report payload so the edge trajectory is visible
    over time (does the CI tighten toward/away from 0 as the holdout accrues?)."""
    r = payload.get("realized", {}) or {}
    tb = payload.get("total_book", {}) or {}
    cc = payload.get("concentration", {}) or {}
    bn = payload.get("beta_neutral", {}) or {}
    a = (bn.get("alpha") or {}) if isinstance(bn, dict) else {}
    cur = conn.execute(
        "INSERT INTO hl_scorecard_snapshots(ts_ms, holdout, realized_n, realized_mean, ci_lo, ci_hi, "
        "spans_zero, total_bps, open_n, alpha_n, alpha_mean, alpha_ci_lo, alpha_ci_hi, "
        "alpha_spans_zero, top1_share, top5_share) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts_ms, int(bool(holdout)), r.get("n"), r.get("mean"), r.get("ci_lo"), r.get("ci_hi"),
         int(bool(r.get("spans_zero"))), tb.get("total_bps"), tb.get("open_n"), bn.get("n"),
         a.get("mean"), a.get("ci_lo"), a.get("ci_hi"), int(bool(a.get("spans_zero"))),
         cc.get("top1_share"), cc.get("top5_share")))
    conn.commit()
    return cur.lastrowid


def get_scorecard_snapshots(conn, *, holdout: bool | None = None, limit: int = 60) -> list[dict]:
    q = "SELECT * FROM hl_scorecard_snapshots"
    args: tuple = ()
    if holdout is not None:
        q += " WHERE holdout=?"
        args = (int(bool(holdout)),)
    q += " ORDER BY ts_ms DESC LIMIT ?"
    return [dict(r) for r in conn.execute(q, args + (limit,)).fetchall()]


def set_meta(conn, key: str, value: str, *, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO hl_meta(key, value, updated_ts_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts_ms=excluded.updated_ts_ms",
        (key, value, ts_ms))
    conn.commit()


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM hl_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_holdout_freeze(conn, freeze_ts_ms: int, *, ts_ms: int | None = None) -> None:
    """Mark the forward-holdout boundary: every copy opened at/after freeze_ts_ms is out-of-sample.
    Judging a post-hoc-selected wallet only on is_holdout=1 rows is the honest OOS test."""
    set_meta(conn, "holdout_freeze_ts_ms", str(int(freeze_ts_ms)), ts_ms=ts_ms or freeze_ts_ms)


def holdout_freeze_ts(conn) -> int | None:
    v = get_meta(conn, "holdout_freeze_ts_ms")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent additive migrations. SQLite has no ADD COLUMN IF NOT EXISTS, so probe first."""
    for table in ("hl_tracked_traders", "hl_screened_traders"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "archetype_pass" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN archetype_pass INTEGER DEFAULT 0")
        # backfill always runs (idempotent: rows already 1 are unchanged)
        conn.execute(f"UPDATE {table} SET archetype_pass=1 WHERE verdict='copyable' AND archetype_pass=0")
    tracked_cols = {r[1] for r in conn.execute("PRAGMA table_info(hl_tracked_traders)")}
    tracked_adds = {
        "lifecycle_status": "TEXT DEFAULT 'active'",
        "lifecycle_reason": "TEXT",
        "lifecycle_updated_ts_ms": "INTEGER",
        "last_seen_ts_ms": "INTEGER",
        "culled_ts_ms": "INTEGER",
    }
    for col, spec in tracked_adds.items():
        if col not in tracked_cols:
            conn.execute(f"ALTER TABLE hl_tracked_traders ADD COLUMN {col} {spec}")
    conn.execute("UPDATE hl_tracked_traders SET lifecycle_status='active' WHERE lifecycle_status IS NULL")
    conn.execute(
        "UPDATE hl_tracked_traders SET last_seen_ts_ms=COALESCE(updated_ts_ms, added_ts_ms) "
        "WHERE last_seen_ts_ms IS NULL AND archetype_pass=1"
    )

    target_cols = {r[1] for r in conn.execute("PRAGMA table_info(hl_copy_targets)")}
    target_adds = {
        "copy_tier": "TEXT DEFAULT 'proven'",
        "reason": "TEXT",
        "updated_ts_ms": "INTEGER",
    }
    for col, spec in target_adds.items():
        if col not in target_cols:
            conn.execute(f"ALTER TABLE hl_copy_targets ADD COLUMN {col} {spec}")
    conn.execute("UPDATE hl_copy_targets SET copy_tier='proven' WHERE copy_tier IS NULL")
    conn.execute(
        "UPDATE hl_copy_targets SET updated_ts_ms=added_ts_ms "
        "WHERE updated_ts_ms IS NULL AND added_ts_ms IS NOT NULL"
    )

    copy_cols = {r[1] for r in conn.execute("PRAGMA table_info(hl_copy_book)")}
    if "copy_tier" not in copy_cols:
        conn.execute("ALTER TABLE hl_copy_book ADD COLUMN copy_tier TEXT DEFAULT 'unknown'")
    conn.execute("UPDATE hl_copy_book SET copy_tier='unknown' WHERE copy_tier IS NULL")

    # Copy-fidelity capture (additive, observational — does NOT change the sizeless pnl_bps).
    # Lets a closed trade be replayed at real deployable size, funding-adjusted, and beta-neutral,
    # and freezes a forward holdout so post-hoc winners are judged out-of-sample.
    copy_cols = {r[1] for r in conn.execute("PRAGMA table_info(hl_copy_book)")}
    copy_adds = {
        "target_entry_px": "REAL",      # target's own fill (entryPx) when we detected the entry
        "target_szi": "REAL",           # target's signed size (conviction proxy)
        "target_notional": "REAL",      # |szi| * entryPx, their position notional at detect
        "entry_spread_bps": "REAL",     # book spread at our entry
        "entry_depth_json": "TEXT",     # compact L2 snapshot at entry (fills.compact_book)
        "exit_spread_bps": "REAL",
        "exit_depth_json": "TEXT",      # compact L2 snapshot at exit
        "funding_bps": "REAL",          # funding paid over the hold (signed cost to us)
        "funding_pnl_bps": "REAL",      # canonical: positive helps PnL, negative hurts
        "funding_provenance_status": "TEXT",
        "source_event_ts_ms": "INTEGER",
        "source_received_ts_ms": "INTEGER",
        "decision_ts_ms": "INTEGER",
        "book_request_ts_ms": "INTEGER",
        "book_received_ts_ms": "INTEGER",
        "mirror_open_ts_ms": "INTEGER",
        "source_latency_status": "TEXT",
        "source_event_tid": "TEXT",
        "mfe_bps": "REAL",              # max favorable excursion over the hold (gross, our side)
        "mae_bps": "REAL",              # max adverse excursion over the hold
        "lag_bps": "REAL",              # detection-lag tax: our entry_mark vs target_entry_px
        "is_holdout": "INTEGER DEFAULT 0",  # 1 if opened after the forward-holdout freeze
        "is_cold_start": "INTEGER DEFAULT 0",  # 1 if mirrored from the target's pre-existing book
    }
    for col, spec in copy_adds.items():
        if col not in copy_cols:
            conn.execute(f"ALTER TABLE hl_copy_book ADD COLUMN {col} {spec}")
    conn.execute(
        "UPDATE hl_copy_book SET source_latency_status='historical_not_captured' "
        "WHERE source_latency_status IS NULL"
    )

    signal_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(hl_altshort_signal_observations)")
    }
    signal_adds = {
        "funding_pnl_bps": "REAL",
        "funding_provenance_status": "TEXT",
    }
    for col, spec in signal_adds.items():
        if signal_cols and col not in signal_cols:
            conn.execute(f"ALTER TABLE hl_altshort_signal_observations ADD COLUMN {col} {spec}")

    regime_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(hl_altshort_regime_marks)")
    }
    regime_adds = {
        "funding_rate": "REAL",
        "open_mark": "REAL",
        "high_mark": "REAL",
        "low_mark": "REAL",
    }
    for col, spec in regime_adds.items():
        if regime_cols and col not in regime_cols:
            conn.execute(f"ALTER TABLE hl_altshort_regime_marks ADD COLUMN {col} {spec}")
    if regime_cols:
        conn.execute(
            "UPDATE hl_altshort_regime_marks SET "
            "open_mark=COALESCE(open_mark, mark), high_mark=COALESCE(high_mark, mark), "
            "low_mark=COALESCE(low_mark, mark) "
            "WHERE open_mark IS NULL OR high_mark IS NULL OR low_mark IS NULL"
        )
    conn.execute("UPDATE hl_copy_book SET is_holdout=0 WHERE is_holdout IS NULL")
    conn.commit()


# ── screened traders (full graded list for the analyzer tab) ──────────────────
def upsert_screened(conn, wallet: str, info: dict, *, ts_ms: int) -> None:
    archetype_pass = int(bool(info["archetype_pass"] if "archetype_pass" in info else info.get("verdict") == "copyable"))
    conn.execute(
        "INSERT INTO hl_screened_traders(wallet, verdict, archetype, taker_frac, profit_factor, "
        "win_rate, n_closes, top_coin_frac, single_fill_frac, month_roi, realized, account_value, "
        "reason, archetype_pass, ts_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(wallet) DO UPDATE SET verdict=excluded.verdict, archetype=excluded.archetype, "
        "taker_frac=excluded.taker_frac, profit_factor=excluded.profit_factor, win_rate=excluded.win_rate, "
        "n_closes=excluded.n_closes, top_coin_frac=excluded.top_coin_frac, "
        "single_fill_frac=excluded.single_fill_frac, month_roi=excluded.month_roi, "
        "realized=excluded.realized, account_value=excluded.account_value, reason=excluded.reason, "
        "archetype_pass=excluded.archetype_pass, ts_ms=excluded.ts_ms",
        (wallet, info.get("verdict"), info.get("archetype"), info.get("taker_frac"),
         info.get("profit_factor"), info.get("win_rate"), info.get("n_closes"),
         info.get("top_coin_frac"), info.get("single_fill_frac"), info.get("month_roi"),
         info.get("realized"), info.get("account_value"), info.get("reason"), archetype_pass, ts_ms))
    conn.commit()


def get_screened(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_screened_traders ORDER BY archetype_pass DESC, month_roi DESC").fetchall()]


def display_verdict(verdict: str | None) -> str | None:
    """Legacy `copyable` meant phase-1 archetype-pass. Public displays call that `watch`."""
    return "watch" if verdict == "copyable" else verdict


def normalized_verdict_counts(verdicts: dict | None) -> dict:
    out: dict[str, int] = {}
    for k, v in (verdicts or {}).items():
        key = display_verdict(k) or "unknown"
        out[key] = out.get(key, 0) + int(v or 0)
    return out


def _candidate_row(conn, wallet: str) -> dict | None:
    for table, source in (("hl_tracked_traders", "tracked"), ("hl_screened_traders", "screened")):
        row = conn.execute(f"SELECT * FROM {table} WHERE wallet=?", (wallet,)).fetchone()
        if row:
            d = dict(row)
            d["source_table"] = source
            return d
    return None


def has_proven_wallet(conn, *, now_ms: int, follow_stats: dict | None = None,
                      copy_stats: dict | None = None) -> bool:
    """Does any non-culled, copy-eligible archetype-pass wallet have phase-2 proof?"""
    from sniper.hyperlive import trader_intel as TI

    fstats = follow_stats if follow_stats is not None else follow_stats_by_wallet(conn)
    cstats = copy_stats if copy_stats is not None else copy_stats_by_wallet(conn)
    seen = set()
    rows = []
    for table in ("hl_tracked_traders", "hl_screened_traders"):
        for r in conn.execute(f"SELECT * FROM {table}").fetchall():
            w = r["wallet"]
            if w in seen:
                continue
            seen.add(w)
            rows.append(dict(r))
    for row in rows:
        if row.get("lifecycle_status") == "culled":
            continue
        if not (row.get("archetype_pass") in (1, True) or row.get("verdict") == "copyable"):
            continue
        if copy_disqualification_reason(cstats.get(row["wallet"])):
            continue
        phase = TI.copyability_phase2(fstats.get(row["wallet"]), now_ms=now_ms)
        if phase["phase2"] == "proven":
            return True
    return False


def copy_disqualification_reason(stats: dict | None) -> str | None:
    """Paper-copy result threshold that immediately blocks new mirror opens."""
    s = stats or {}
    n_closed = int(s.get("n_closed", 0) or 0)
    mean = float(s.get("mean_pnl_bps", 0.0) or 0.0)
    if n_closed >= COPY_MATURE_CULL_MIN_CLOSES and mean <= 0.0:
        return f"{n_closed} closed copies mean {mean:+.2f}bps <= 0"
    if n_closed >= COPY_EARLY_CULL_MIN_CLOSES and mean <= COPY_EARLY_CULL_MAX_MEAN_BPS:
        return f"{n_closed} closed copies mean {mean:+.2f}bps <= {COPY_EARLY_CULL_MAX_MEAN_BPS:g}"
    return None


def copy_target_status(conn, wallet: str, *, now_ms: int, follow_stats: dict | None = None,
                       copy_stats: dict | None = None, copy_tier: str | None = None,
                       proven_exists: bool | None = None) -> dict:
    """Eligibility for paper mirror-copy.

    `proven` targets require phase-2 proof. `watch_fallback` targets are executable only
    when no wallet is proven and the lifecycle manager selected the wallet into the basket.
    """
    from sniper.hyperlive import trader_intel as TI

    tier = copy_tier or "proven"
    row = _candidate_row(conn, wallet)
    if not row:
        return {"wallet": wallet, "eligible": False, "copy_tier": tier,
                "effective_verdict": "unknown", "lifecycle_status": "unknown",
                "reason": "wallet not screened/tracked"}
    stored = row.get("verdict")
    archetype_pass = row.get("archetype_pass")
    fstats = follow_stats if follow_stats is not None else follow_stats_by_wallet(conn)
    if copy_stats is None:
        copy_stats = copy_stats_by_wallet(conn)
    fs = fstats.get(wallet) if fstats else None
    cs = copy_stats.get(wallet) if copy_stats else None
    is_archetype_pass = archetype_pass in (1, True) or stored == "copyable"
    if is_archetype_pass:
        phase = TI.copyability_phase2(fs, now_ms=now_ms)
        effective = phase["phase2"]
        reason = phase["reason"]
        n_closed = phase["n_closed"]
    else:
        effective = display_verdict(stored) or "unknown"
        reason = row.get("reason") or "not archetype-pass"
        n_closed = int((fs or {}).get("n_closed", 0))
    lifecycle_status = row.get("lifecycle_status") or "active"
    lifecycle_reason = row.get("lifecycle_reason")
    copy_disqualified = copy_disqualification_reason(cs)
    eligible = effective == "proven"
    if lifecycle_status == "culled":
        eligible = False
        reason = lifecycle_reason or "wallet culled"
    elif copy_disqualified:
        eligible = False
        reason = copy_disqualified
    elif tier == "watch_fallback":
        if proven_exists is None:
            proven_exists = has_proven_wallet(conn, now_ms=now_ms, follow_stats=fstats,
                                              copy_stats=copy_stats)
        if proven_exists:
            eligible = False
            reason = "proven wallet exists; fallback disabled"
        elif effective != "watch" or not is_archetype_pass:
            eligible = False
        else:
            eligible = True
            reason = "watch fallback active; no proven wallets"
    elif tier != "proven":
        eligible = False
        reason = f"unknown copy tier {tier}"
    return {
        "wallet": wallet,
        "eligible": eligible,
        "copy_tier": tier,
        "effective_verdict": effective,
        "stored_verdict": stored,
        "display_verdict": display_verdict(stored),
        "archetype_pass": int(bool(is_archetype_pass)),
        "n_closed": n_closed,
        "lifecycle_status": lifecycle_status,
        "lifecycle_reason": lifecycle_reason,
        "copy_stats": cs,
        "reason": reason,
    }


# ── copy targets + paper mirror book ──────────────────────────────────────────
def set_copy_target(conn, wallet: str, *, active: bool = True, label: str | None = None,
                    copy_tier: str = "proven", reason: str | None = None, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO hl_copy_targets(wallet, active, label, added_ts_ms, copy_tier, reason, updated_ts_ms) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(wallet) DO UPDATE SET active=excluded.active, "
        "label=COALESCE(excluded.label, label), copy_tier=excluded.copy_tier, "
        "reason=COALESCE(excluded.reason, reason), updated_ts_ms=excluded.updated_ts_ms",
        (wallet, 1 if active else 0, label, ts_ms, copy_tier, reason, ts_ms))
    conn.commit()


def get_copy_targets(conn, *, active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM hl_copy_targets"
    if active_only:
        q += " WHERE active=1"
    return [dict(r) for r in conn.execute(q).fetchall()]


def get_proven_copy_targets(conn, *, now_ms: int) -> list[dict]:
    fstats = follow_stats_by_wallet(conn)
    cstats = copy_stats_by_wallet(conn)
    out = []
    for target in get_copy_targets(conn, active_only=True):
        if (target.get("copy_tier") or "proven") != "proven":
            continue
        status = copy_target_status(conn, target["wallet"], now_ms=now_ms, follow_stats=fstats,
                                    copy_stats=cstats, copy_tier="proven")
        if status["eligible"]:
            out.append({**target, "copy_status": status})
    return out


PROOF_LANE_MIN_FOLLOW_CLOSES = 5


def get_executable_copy_targets(conn, *, now_ms: int, proof_lane_n: int | None = None) -> list[dict]:
    fstats = follow_stats_by_wallet(conn)
    cstats = copy_stats_by_wallet(conn)
    proven_exists = has_proven_wallet(conn, now_ms=now_ms, follow_stats=fstats, copy_stats=cstats)
    out = []
    for target in get_copy_targets(conn, active_only=True):
        status = copy_target_status(
            conn, target["wallet"], now_ms=now_ms, follow_stats=fstats,
            copy_stats=cstats, copy_tier=target.get("copy_tier") or "proven",
            proven_exists=proven_exists)
        if status["eligible"]:
            out.append({**target, "copy_status": status})
    # Proof lane (2026-07-17): promotion used to freeze EVERY fallback's exact-copy
    # evidence ("proven wallet exists; fallback disabled") — so no second wallet could
    # accrue the copy record needed to prove out. Keep the top-N watch_fallback wallets
    # (by follow mean, evidence floor n>=5) executable for PAPER copy. Rows keep
    # copy_tier='watch_fallback', so the real leg (proven-only eligibility) ignores them.
    if proof_lane_n is None:
        proof_lane_n = int(os.getenv("HL_COPY_PROOF_LANE_N", "8"))
    if proven_exists and proof_lane_n > 0:
        chosen = {t["wallet"] for t in out}
        cands = []
        for target in get_copy_targets(conn, active_only=False):
            w = target["wallet"]
            if w in chosen or (target.get("copy_tier") or "proven") != "watch_fallback":
                continue
            status = copy_target_status(conn, w, now_ms=now_ms, follow_stats=fstats,
                                        copy_stats=cstats, copy_tier="watch_fallback",
                                        proven_exists=False)   # as-if-no-proven quality bar
            if not status["eligible"]:
                continue
            fs = fstats.get(w) or {}
            if int(fs.get("n_closed", 0)) < PROOF_LANE_MIN_FOLLOW_CLOSES:
                continue
            mean = float(fs.get("mean_net_bps", 0.0))
            if mean <= 0:
                continue      # negative-mean = cull candidate; don't spend a lane slot
            cands.append((mean, target, status))
        cands.sort(key=lambda x: -x[0])
        for _, target, status in cands[:proof_lane_n]:
            out.append({**target, "copy_status": {
                **status, "proof_lane": True,
                "reason": "proof_lane: exact-copy evidence accrual"}})
    return out


def open_copy(conn, target: str, coin: str, side: str, entry_mark: float, *,
              copy_tier: str = "unknown", ts_ms: int,
              target_entry_px: float | None = None, target_szi: float | None = None,
              target_notional: float | None = None, entry_spread_bps: float | None = None,
              entry_depth_json: str | None = None, lag_bps: float | None = None,
              is_holdout: int = 0, source_event_ts_ms: int | None = None,
              source_received_ts_ms: int | None = None, decision_ts_ms: int | None = None,
              book_request_ts_ms: int | None = None, book_received_ts_ms: int | None = None,
              mirror_open_ts_ms: int | None = None, source_latency_status: str | None = None,
              source_event_tid: str | None = None) -> int:
    """Open a paper mirror. Capture fields are optional/observational — a caller that can't fetch
    the target fill or book snapshot still opens a valid row (older behavior)."""
    cur = conn.execute(
        "INSERT INTO hl_copy_book(target, coin, side, entry_mark, detect_ts_ms, status, copy_tier, "
        "target_entry_px, target_szi, target_notional, entry_spread_bps, entry_depth_json, lag_bps, "
        "is_holdout,source_event_ts_ms,source_received_ts_ms,decision_ts_ms,book_request_ts_ms,"
        "book_received_ts_ms,mirror_open_ts_ms,source_latency_status,source_event_tid) "
        "VALUES (?,?,?,?,?, 'open', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (target, coin, side, entry_mark, ts_ms, copy_tier or "unknown", target_entry_px, target_szi,
         target_notional, entry_spread_bps, entry_depth_json, lag_bps, int(is_holdout or 0),
         source_event_ts_ms, source_received_ts_ms, decision_ts_ms, book_request_ts_ms,
         book_received_ts_ms, mirror_open_ts_ms, source_latency_status, source_event_tid))
    conn.commit()
    return cur.lastrowid


def stamp_copy_mirror_open(conn, copy_id: int, *, committed_ts_ms: int) -> None:
    """Stamp after the initial row commit; latency never claims pre-commit persistence."""
    conn.execute(
        "UPDATE hl_copy_book SET mirror_open_ts_ms=? WHERE id=? AND mirror_open_ts_ms IS NULL",
        (int(committed_ts_ms), int(copy_id)),
    )
    conn.commit()


def record_copy_source_events(conn, events: list[dict]) -> int:
    inserted = 0
    for event in events:
        snapshot = int(bool(event.get("is_snapshot")))
        cur = conn.execute(
            "INSERT OR IGNORE INTO hl_copy_source_events("
            "wallet,tid,coin,side,direction,px,sz,start_position,oid,tx_hash,"
            "source_event_ts_ms,received_ts_ms,clock_domain,is_snapshot,is_recovery,"
            "connection_epoch,persisted_ts_ms,match_status,match_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(event["wallet"]).lower(), str(event["tid"]), str(event["coin"]),
             event.get("side"), event.get("direction"), event.get("px"), event.get("sz"),
             event.get("start_position"), event.get("oid"), event.get("tx_hash"),
             int(event["source_event_ts_ms"]), int(event["received_ts_ms"]),
             str(event.get("clock_domain") or "corrected_utc"), snapshot,
             int(bool(event.get("is_recovery"))), int(event.get("connection_epoch") or 0),
             int(event.get("persisted_ts_ms") or event["received_ts_ms"]),
             "snapshot_baseline" if snapshot else "unmatched",
             "initial_snapshot" if snapshot else None),
        )
        inserted += int(cur.rowcount > 0)
    conn.commit()
    return inserted


def list_unmatched_copy_source_events(conn, *, wallet: str, after_ts_ms: int,
                                      before_ts_ms: int) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM hl_copy_source_events WHERE wallet=? AND match_status='unmatched' "
        "AND source_event_ts_ms>? AND source_event_ts_ms<=? ORDER BY source_event_ts_ms,tid",
        (str(wallet).lower(), int(after_ts_ms), int(before_ts_ms)),
    ).fetchall()]


def match_copy_source_events(conn, *, copy_id: int, tids: list[str],
                             matched_ts_ms: int) -> None:
    copy = conn.execute("SELECT target FROM hl_copy_book WHERE id=?", (int(copy_id),)).fetchone()
    if copy is None:
        raise ValueError(f"unknown_copy:{copy_id}")
    wallet = str(copy["target"]).lower()
    for tid in tids:
        conn.execute(
            "UPDATE hl_copy_source_events SET copy_id=?,match_status='matched',"
            "match_reason='exact_position_delta',matched_ts_ms=? "
            "WHERE wallet=? AND tid=? AND match_status='unmatched'",
            (int(copy_id), int(matched_ts_ms), wallet, str(tid)),
        )
    conn.commit()


def backfill_copy_source_event(conn, *, copy_id: int, source_event_ts_ms: int,
                               tids: list[str]) -> bool:
    """Add proven exchange event time without inventing any historical receive time."""
    cur = conn.execute(
        "UPDATE hl_copy_book SET source_event_ts_ms=?,source_event_tid=?,"
        "source_latency_status='historical_event_reconstructed' "
        "WHERE id=? AND source_latency_status='historical_not_captured' "
        "AND source_event_ts_ms IS NULL AND source_received_ts_ms IS NULL "
        "AND ?>0 AND ?<=detect_ts_ms",
        (
            int(source_event_ts_ms),
            json.dumps([str(tid) for tid in tids], separators=(",", ":")),
            int(copy_id),
            int(source_event_ts_ms),
            int(source_event_ts_ms),
        ),
    )
    return cur.rowcount == 1


def copy_source_cursors(conn, wallets) -> dict[str, int]:
    out = {}
    for wallet in {str(value).lower() for value in wallets}:
        row = conn.execute(
            "SELECT MAX(source_event_ts_ms) FROM hl_copy_source_events WHERE wallet=?", (wallet,)
        ).fetchone()
        if row and row[0] is not None:
            out[wallet] = int(row[0])
    return out


def copy_source_unmatched_count(conn, wallets) -> int:
    values = sorted({str(value).lower() for value in wallets})
    if not values:
        return 0
    placeholders = ",".join("?" for _ in values)
    return int(conn.execute(
        f"SELECT COUNT(*) FROM hl_copy_source_events WHERE match_status='unmatched' "
        f"AND wallet IN ({placeholders})", values,
    ).fetchone()[0])


def record_copy_source_gaps(conn, gaps: list[dict], *, persisted_ts_ms: int) -> int:
    inserted = 0
    for gap in gaps:
        cur = conn.execute(
            "INSERT OR IGNORE INTO hl_copy_source_gaps("
            "start_received_ts_ms,end_received_ts_ms,connection_epoch,dropped_count,reason,"
            "persisted_ts_ms) VALUES (?,?,?,?,?,?)",
            (int(gap["start_received_ts_ms"]), int(gap["end_received_ts_ms"]),
             int(gap.get("connection_epoch") or 0), int(gap["dropped_count"]),
             str(gap.get("reason") or "queue_overflow"), int(persisted_ts_ms)),
        )
        inserted += int(cur.rowcount > 0)
    conn.commit()
    return inserted


def copy_source_gap_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM hl_copy_source_gaps").fetchone()[0])


def copy_source_gap_overlaps(conn, *, after_ts_ms: int, before_ts_ms: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM hl_copy_source_gaps WHERE end_received_ts_ms>? "
        "AND start_received_ts_ms<=? LIMIT 1", (int(after_ts_ms), int(before_ts_ms)),
    ).fetchone() is not None


def close_copy(conn, copy_id: int, *, exit_mark: float, pnl_bps: float, ts_ms: int,
               exit_spread_bps: float | None = None, exit_depth_json: str | None = None,
               funding_bps: float | None = None,
               funding_pnl_bps: float | None = None,
               funding_provenance_status: str | None = None) -> None:
    if funding_pnl_bps is not None:
        canonical_legacy = -float(funding_pnl_bps)
        if funding_bps is not None and abs(float(funding_bps) - canonical_legacy) > 1e-9:
            raise ValueError("copy funding legacy/canonical sign mismatch")
        funding_bps = canonical_legacy
    conn.execute(
        "UPDATE hl_copy_book SET status='closed', exit_ts_ms=?, exit_mark=?, pnl_bps=?, "
        "exit_spread_bps=COALESCE(?, exit_spread_bps), exit_depth_json=COALESCE(?, exit_depth_json), "
        "funding_bps=COALESCE(?, funding_bps), "
        "funding_pnl_bps=COALESCE(?, funding_pnl_bps), "
        "funding_provenance_status=COALESCE(?, funding_provenance_status) WHERE id=?",
        (ts_ms, exit_mark, pnl_bps, exit_spread_bps, exit_depth_json, funding_bps,
         funding_pnl_bps, funding_provenance_status, copy_id))
    conn.commit()


def update_copy_excursion(conn, copy_id: int, *, gross_bps: float) -> None:
    """Track intra-hold MFE/MAE for an open mirror. Called each cycle with the current gross
    (our-side) bps; keeps the running max favorable and max adverse so we can later study whether
    a stop/TP would have helped — a question entry+exit marks alone can't answer."""
    row = conn.execute("SELECT mfe_bps, mae_bps FROM hl_copy_book WHERE id=?", (copy_id,)).fetchone()
    if row is None:
        return
    mfe = gross_bps if row["mfe_bps"] is None else max(row["mfe_bps"], gross_bps)
    mae = gross_bps if row["mae_bps"] is None else min(row["mae_bps"], gross_bps)
    if mfe == row["mfe_bps"] and mae == row["mae_bps"]:
        return
    conn.execute("UPDATE hl_copy_book SET mfe_bps=?, mae_bps=? WHERE id=?", (mfe, mae, copy_id))
    conn.commit()


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def create_altshort_arm(conn, manifest: dict, *, activated_ts_ms: int, code_sha: str,
                        created_ts_ms: int) -> dict:
    """Create an immutable paper-arm identity; identical retries are idempotent."""
    required = ("arm_id", "policy", "targets", "taxonomy_version",
                "benchmark_version", "regime_version")
    missing = [field for field in required if field not in manifest]
    if missing:
        raise ValueError(f"arm manifest missing: {','.join(missing)}")
    arm_id = str(manifest["arm_id"])
    policy_json = _canonical_json(manifest["policy"])
    targets = sorted({str(value).strip().lower() for value in manifest["targets"] if str(value).strip()})
    targets_json = _canonical_json(targets)
    manifest_json = _canonical_json(manifest)
    policy_hash = hashlib.sha256(policy_json.encode("utf-8")).hexdigest()
    identity = {
        "arm_id": arm_id,
        "activated_ts_ms": int(activated_ts_ms),
        "code_sha": str(code_sha),
        "policy_json": policy_json,
        "policy_hash": policy_hash,
        "targets_json": targets_json,
        "taxonomy_version": str(manifest["taxonomy_version"]),
        "benchmark_version": str(manifest["benchmark_version"]),
        "regime_version": str(manifest["regime_version"]),
        "manifest_json": manifest_json,
    }
    existing = conn.execute("SELECT * FROM hl_altshort_arms WHERE arm_id=?", (arm_id,)).fetchone()
    if existing:
        row = dict(existing)
        if any(row[key] != value for key, value in identity.items()):
            raise ValueError(f"immutable arm conflict: {arm_id}")
        return row
    conn.execute(
        "INSERT INTO hl_altshort_arms(arm_id, activated_ts_ms, code_sha, policy_json, "
        "policy_hash, targets_json, taxonomy_version, benchmark_version, regime_version, "
        "manifest_json, created_ts_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (*identity.values(), int(created_ts_ms)),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM hl_altshort_arms WHERE arm_id=?", (arm_id,)).fetchone())


def get_altshort_arm(conn, arm_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM hl_altshort_arms WHERE arm_id=?", (arm_id,)).fetchone()
    return dict(row) if row else None


_ALTSHORT_SEMANTIC_FIELDS = (
    "policy_hash",
    "targets_json",
    "taxonomy_version",
    "benchmark_version",
    "regime_version",
)


def _altshort_manifest_semantics(manifest: dict) -> tuple:
    policy_json = _canonical_json(manifest["policy"])
    targets = sorted({
        str(value).strip().lower()
        for value in manifest["targets"]
        if str(value).strip()
    })
    return (
        hashlib.sha256(policy_json.encode("utf-8")).hexdigest(),
        _canonical_json(targets),
        str(manifest["taxonomy_version"]),
        str(manifest["benchmark_version"]),
        str(manifest["regime_version"]),
    )


def find_compatible_altshort_arm(conn, manifest: dict) -> dict | None:
    values = _altshort_manifest_semantics(manifest)
    where = " AND ".join(f"{field}=?" for field in _ALTSHORT_SEMANTIC_FIELDS)
    row = conn.execute(
        f"SELECT * FROM hl_altshort_arms WHERE {where} "
        "ORDER BY activated_ts_ms, created_ts_ms, arm_id LIMIT 1",
        values,
    ).fetchone()
    return dict(row) if row else None


def compatible_altshort_arm_ids(conn, *, arm_id: str) -> list[str]:
    arm = get_altshort_arm(conn, arm_id)
    if arm is None:
        return []
    where = " AND ".join(f"{field}=?" for field in _ALTSHORT_SEMANTIC_FIELDS)
    values = tuple(arm[field] for field in _ALTSHORT_SEMANTIC_FIELDS)
    return [
        str(row["arm_id"])
        for row in conn.execute(
            f"SELECT arm_id FROM hl_altshort_arms WHERE {where} "
            "ORDER BY activated_ts_ms, created_ts_ms, arm_id",
            values,
        )
    ]


def record_altshort_observation(conn, *, arm_id: str, copy_id: int,
                                regime: dict, benchmark: dict) -> dict:
    from sniper.hyperlive.altshort_universe import is_alt_short

    arm = get_altshort_arm(conn, arm_id)
    if arm is None:
        raise ValueError(f"unknown_arm:{arm_id}")
    copy = conn.execute("SELECT * FROM hl_copy_book WHERE id=?", (copy_id,)).fetchone()
    if copy is None:
        raise ValueError(f"unknown_copy:{copy_id}")
    copy = dict(copy)
    if int(copy["detect_ts_ms"]) < int(arm["activated_ts_ms"]):
        raise ValueError("pre_arm_epoch")
    if str(copy["target"]).lower() not in json.loads(arm["targets_json"]):
        raise ValueError("target_not_in_arm")
    if not is_alt_short(copy.get("coin"), copy.get("side")):
        raise ValueError("not_alt_short")
    regime_json = _canonical_json(regime)
    benchmark_json = _canonical_json(benchmark)
    existing = conn.execute(
        "SELECT * FROM hl_altshort_observations WHERE copy_id=?", (copy_id,)
    ).fetchone()
    if existing:
        row = dict(existing)
        expected = {
            "arm_id": arm_id,
            "regime_json": regime_json,
            "benchmark_entry_json": benchmark_json,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise ValueError(f"observation conflict:{copy_id}")
        return row
    conn.execute(
        "INSERT INTO hl_altshort_observations(copy_id, arm_id, target, coin, side, "
        "detect_ts_ms, entry_mark, regime_json, benchmark_entry_json, latest_mark, "
        "latest_mark_ts_ms, status) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')",
        (copy_id, arm_id, copy["target"], provider_coin(copy["coin"]), copy["side"],
         copy["detect_ts_ms"], copy["entry_mark"], regime_json, benchmark_json,
         copy["entry_mark"], copy["detect_ts_ms"]),
    )
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM hl_altshort_observations WHERE copy_id=?", (copy_id,)
    ).fetchone())


def update_altshort_observation_mark(conn, *, copy_id: int, mark: float, ts_ms: int) -> None:
    if float(mark) <= 0:
        return
    conn.execute(
        "UPDATE hl_altshort_observations SET latest_mark=?, latest_mark_ts_ms=? "
        "WHERE copy_id=? AND status='open' AND "
        "(latest_mark_ts_ms IS NULL OR latest_mark_ts_ms<=?)",
        (float(mark), int(ts_ms), copy_id, int(ts_ms)),
    )
    conn.commit()


def close_altshort_observation(conn, *, copy_id: int, exit_mark: float,
                               exit_ts_ms: int, benchmark: dict,
                               extra_cost_bps: float = 0.0,
                               funding_complete: bool = True) -> None:
    copy = conn.execute(
        "SELECT pnl_bps, funding_bps, funding_pnl_bps, funding_provenance_status "
        "FROM hl_copy_book WHERE id=?", (copy_id,)
    ).fetchone()
    if copy is None:
        raise ValueError(f"unknown_copy:{copy_id}")
    if copy["funding_provenance_status"] in {"complete", "not_applicable"}:
        contribution = float(copy["funding_pnl_bps"] or 0.0)
    elif copy["funding_provenance_status"] is None and copy["funding_bps"] is not None:
        contribution = -float(copy["funding_bps"])
    elif funding_complete:
        contribution = 0.0
    else:
        contribution = None
    wallet_net_bps = (
        float(copy["pnl_bps"]) - float(extra_cost_bps) + contribution
        if funding_complete and contribution is not None else None
    )
    conn.execute(
        "UPDATE hl_altshort_observations SET status='closed', exit_mark=?, exit_ts_ms=?, "
        "wallet_net_bps=?, benchmark_exit_json=?, latest_mark=?, latest_mark_ts_ms=? "
        ", provenance_status=? WHERE copy_id=?",
        (float(exit_mark), int(exit_ts_ms), wallet_net_bps, _canonical_json(benchmark),
         float(exit_mark), int(exit_ts_ms),
         "valid" if funding_complete else "missing_signal_funding", copy_id),
    )
    conn.commit()


def list_altshort_observations(conn, *, arm_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM hl_altshort_observations WHERE arm_id=? ORDER BY detect_ts_ms, copy_id",
        (arm_id,),
    ).fetchall()]


def get_altshort_observation(conn, *, copy_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM hl_altshort_observations WHERE copy_id=?", (copy_id,)
    ).fetchone()
    return dict(row) if row else None


def record_altshort_signal_decision(
    conn, *, arm_id: str, coin: str, decision_ts_ms: int, decision_mark: float,
    runup: float, regime: dict, benchmark_entry: dict, gated_in: bool,
) -> dict:
    arm = get_altshort_arm(conn, arm_id)
    if arm is None:
        raise ValueError(f"unknown_arm:{arm_id}")
    if int(decision_ts_ms) < int(arm["activated_ts_ms"]):
        raise ValueError("pre_arm_epoch")
    symbol = provider_coin(coin)
    if symbol is None:
        raise ValueError("missing_coin")
    identity = {
        "decision_mark": float(decision_mark),
        "runup": float(runup),
        "regime_json": _canonical_json(regime),
        "benchmark_entry_json": _canonical_json(benchmark_entry),
        "gated_in": int(bool(gated_in)),
        "status": "pending" if gated_in else "stood_down",
    }
    existing = conn.execute(
        "SELECT * FROM hl_altshort_signal_observations "
        "WHERE arm_id=? AND coin=? AND decision_ts_ms=?",
        (arm_id, symbol, int(decision_ts_ms)),
    ).fetchone()
    if existing:
        row = dict(existing)
        if any(row[key] != value for key, value in identity.items()):
            raise ValueError(f"signal decision conflict:{arm_id}:{symbol}:{decision_ts_ms}")
        return row
    conn.execute(
        "INSERT INTO hl_altshort_signal_observations("
        "arm_id, coin, decision_ts_ms, decision_mark, runup, regime_json, "
        "benchmark_entry_json, gated_in, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (arm_id, symbol, int(decision_ts_ms), *identity.values()),
    )
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM hl_altshort_signal_observations "
        "WHERE arm_id=? AND coin=? AND decision_ts_ms=?",
        (arm_id, symbol, int(decision_ts_ms)),
    ).fetchone())


def record_altshort_signal_regime(
    conn, *, arm_id: str, decision_ts_ms: int, regime: dict,
) -> dict:
    arm = get_altshort_arm(conn, arm_id)
    if arm is None:
        raise ValueError(f"unknown_arm:{arm_id}")
    if int(decision_ts_ms) < int(arm["activated_ts_ms"]):
        raise ValueError("pre_arm_epoch")
    regime_json = _canonical_json(regime)
    existing = conn.execute(
        "SELECT * FROM hl_altshort_signal_regimes WHERE arm_id=? AND decision_ts_ms=?",
        (arm_id, int(decision_ts_ms)),
    ).fetchone()
    if existing:
        row = dict(existing)
        if row["regime_json"] != regime_json:
            raise ValueError(f"signal regime conflict:{arm_id}:{decision_ts_ms}")
        return row
    conn.execute(
        "INSERT INTO hl_altshort_signal_regimes(arm_id,decision_ts_ms,regime_json) "
        "VALUES (?,?,?)",
        (arm_id, int(decision_ts_ms), regime_json),
    )
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM hl_altshort_signal_regimes WHERE arm_id=? AND decision_ts_ms=?",
        (arm_id, int(decision_ts_ms)),
    ).fetchone())


def list_altshort_signal_regimes(conn, *, arm_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM hl_altshort_signal_regimes WHERE arm_id=? ORDER BY decision_ts_ms",
        (arm_id,),
    ).fetchall()]


def get_altshort_signal(conn, *, signal_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM hl_altshort_signal_observations WHERE id=?", (int(signal_id),)
    ).fetchone()
    return dict(row) if row else None


def list_altshort_signals(conn, *, arm_id: str, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM hl_altshort_signal_observations WHERE arm_id=?"
    args: tuple = (arm_id,)
    if status is not None:
        query += " AND status=?"
        args += (status,)
    query += " ORDER BY decision_ts_ms, coin"
    return [dict(row) for row in conn.execute(query, args).fetchall()]


def open_altshort_signal(conn, *, signal_id: int, entry_ts_ms: int,
                         entry_mark: float, benchmark_entry: dict | None = None) -> None:
    row = get_altshort_signal(conn, signal_id=signal_id)
    if row is None or row["status"] != "pending":
        raise ValueError("signal_not_pending")
    next_hour = (int(row["decision_ts_ms"]) // 3_600_000 + 1) * 3_600_000
    if int(entry_ts_ms) // 3_600_000 * 3_600_000 != next_hour:
        raise ValueError("entry_not_next_hour")
    if float(entry_mark) <= 0:
        raise ValueError("invalid_entry_mark")
    conn.execute(
        "UPDATE hl_altshort_signal_observations SET status='open', entry_ts_ms=?, "
        "entry_mark=?, benchmark_entry_json=?, latest_mark=?, latest_mark_ts_ms=? "
        "WHERE id=? AND status='pending'",
        (int(entry_ts_ms), float(entry_mark),
         _canonical_json(benchmark_entry or json.loads(row["benchmark_entry_json"])),
         float(entry_mark), int(entry_ts_ms), int(signal_id)),
    )
    conn.commit()


def reject_altshort_signal(conn, *, signal_id: int, reason: str, ts_ms: int) -> None:
    row = get_altshort_signal(conn, signal_id=signal_id)
    if row is None or row["status"] != "pending":
        raise ValueError("signal_not_pending")
    conn.execute(
        "UPDATE hl_altshort_signal_observations SET status='rejected', exit_reason=?, "
        "exit_ts_ms=? WHERE id=?",
        (str(reason), int(ts_ms), int(signal_id)),
    )
    conn.commit()


def update_altshort_signal_mark(conn, *, signal_id: int, mark: float, ts_ms: int) -> None:
    if float(mark) <= 0:
        return
    conn.execute(
        "UPDATE hl_altshort_signal_observations SET latest_mark=?, latest_mark_ts_ms=? "
        "WHERE id=? AND status='open' AND "
        "(latest_mark_ts_ms IS NULL OR latest_mark_ts_ms<=?)",
        (float(mark), int(ts_ms), int(signal_id), int(ts_ms)),
    )
    conn.commit()


def close_altshort_signal(
    conn, *, signal_id: int, exit_ts_ms: int, exit_mark: float, exit_reason: str,
    funding_bps: float | None = None, funding_pnl_bps: float | None = None,
    funding_provenance_status: str | None = None, benchmark: dict, cost_bps: float,
    provenance_status: str = "valid",
) -> None:
    row = get_altshort_signal(conn, signal_id=signal_id)
    if row is None or row["status"] != "open":
        raise ValueError("signal_not_open")
    gross = (float(row["entry_mark"]) - float(exit_mark)) / float(row["entry_mark"]) * 10_000.0
    contribution = (
        float(funding_pnl_bps) if funding_pnl_bps is not None
        else float(funding_bps) if funding_bps is not None else None
    )
    if funding_bps is not None and contribution is not None \
            and abs(float(funding_bps) - contribution) > 1e-9:
        raise ValueError("signal funding legacy/canonical sign mismatch")
    legacy = contribution
    funding_status = funding_provenance_status or (
        "complete" if contribution is not None else "missing"
    )
    net = gross + contribution - float(cost_bps) if contribution is not None else None
    conn.execute(
        "UPDATE hl_altshort_signal_observations SET status='closed', exit_ts_ms=?, "
        "exit_mark=?, exit_reason=?, gross_bps=?, funding_bps=?, funding_pnl_bps=?, "
        "funding_provenance_status=?, net_bps=?, "
        "benchmark_exit_json=?, latest_mark=?, latest_mark_ts_ms=?, provenance_status=? "
        "WHERE id=?",
        (int(exit_ts_ms), float(exit_mark), str(exit_reason), gross, legacy, contribution,
         funding_status, net,
         _canonical_json(benchmark), float(exit_mark), int(exit_ts_ms),
         str(provenance_status), int(signal_id)),
    )
    conn.commit()


def record_altshort_regime_marks(conn, marks: dict, *, ts_ms: int,
                                 funding_rates: dict | None = None) -> None:
    bucket_ms = int(ts_ms) // 3_600_000 * 3_600_000
    rows = []
    for coin, mark in marks.items():
        try:
            value = float(mark)
        except (TypeError, ValueError):
            continue
        symbol = provider_coin(coin)
        if symbol and value > 0:
            rate = None
            try:
                if funding_rates is not None and coin in funding_rates:
                    rate = float(funding_rates[coin])
                elif funding_rates is not None and symbol in funding_rates:
                    rate = float(funding_rates[symbol])
            except (TypeError, ValueError):
                rate = None
            rows.append((bucket_ms, symbol, value, value, value, value, rate))
    conn.executemany(
        "INSERT INTO hl_altshort_regime_marks("
        "ts_ms, coin, mark, open_mark, high_mark, low_mark, funding_rate) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(ts_ms, coin) DO UPDATE SET mark=excluded.mark, "
        "high_mark=MAX(hl_altshort_regime_marks.high_mark, excluded.mark), "
        "low_mark=MIN(hl_altshort_regime_marks.low_mark, excluded.mark), "
        "funding_rate=COALESCE(excluded.funding_rate, hl_altshort_regime_marks.funding_rate)",
        rows,
    )
    conn.execute(
        "DELETE FROM hl_altshort_regime_marks WHERE ts_ms < ?",
        (bucket_ms - 32 * 86_400_000,),
    )
    conn.commit()


def load_altshort_regime_bars(conn, *, start_ts_ms: int, end_ts_ms: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT ts_ms, coin, mark, open_mark, high_mark, low_mark, funding_rate "
        "FROM hl_altshort_regime_marks "
        "WHERE ts_ms BETWEEN ? AND ? ORDER BY ts_ms, coin",
        (int(start_ts_ms), int(end_ts_ms)),
    ):
        bar = {
            "ts_ms": row["ts_ms"], "open": row["open_mark"],
            "high": row["high_mark"], "low": row["low_mark"], "close": row["mark"],
        }
        if row["funding_rate"] is not None:
            bar["funding_rate"] = row["funding_rate"]
        out.setdefault(row["coin"], []).append(bar)
    return out


def get_open_copies(conn, target: str | None = None) -> list[dict]:
    q = "SELECT * FROM hl_copy_book WHERE status='open'"
    args = ()
    if target:
        q += " AND target=?"
        args = (target,)
    return [dict(r) for r in conn.execute(q + " ORDER BY detect_ts_ms DESC", args).fetchall()]


def recent_closed_copies(conn, limit: int = 30) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_copy_book WHERE status='closed' ORDER BY exit_ts_ms DESC LIMIT ?",
        (limit,)).fetchall()]


def copy_summary(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN pnl_bps>0 THEN 1 ELSE 0 END),0) wins, "
        "COALESCE(SUM(pnl_bps),0) s, COALESCE(AVG(pnl_bps),0) a FROM hl_copy_book WHERE status='closed'"
    ).fetchone()
    open_rows = conn.execute(
        "SELECT b.coin,b.side,b.entry_mark,m.mark FROM hl_copy_book b LEFT JOIN "
        "hl_altshort_regime_marks m ON m.coin=b.coin AND m.ts_ms=(SELECT MAX(mm.ts_ms) "
        "FROM hl_altshort_regime_marks mm WHERE mm.coin=b.coin) WHERE b.status='open'"
    ).fetchall()
    open_values = []
    for opened in open_rows:
        if not opened["entry_mark"] or opened["mark"] is None:
            continue
        change = (float(opened["mark"]) - float(opened["entry_mark"])) / float(opened["entry_mark"])
        gross = change * 10_000 * (-1 if opened["side"] == "short" else 1)
        open_values.append(gross - 9.0)
    closed_values = [float(r[0]) for r in conn.execute(
        "SELECT pnl_bps FROM hl_copy_book WHERE status='closed' AND pnl_bps IS NOT NULL"
    )]
    top = max(closed_values) if closed_values else None
    episodes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT coin,date(detect_ts_ms/1000,'unixepoch') "
        "FROM hl_copy_book WHERE status='closed' AND pnl_bps IS NOT NULL GROUP BY 1,2)"
    ).fetchone()[0]
    open_complete = len(open_values) == len(open_rows)
    open_mtm = round(sum(open_values), 1) if open_complete else None
    total = round(float(row["s"]) + sum(open_values), 1) if open_complete else None
    realized_sum = round(row["s"], 1)
    return {"closed": row["n"], "open": len(open_rows), "wins": row["wins"],
            "win_rate": round(row["wins"] / row["n"], 3) if row["n"] else 0.0,
            "sum_pnl_bps": realized_sum, "realized_sum_bps": realized_sum,
            "avg_pnl_bps": round(row["a"], 2), "open_mtm_bps": open_mtm,
            "open_marked_n": len(open_values), "total_bps": total,
            "top1_share": round(top / float(row["s"]), 3) if top is not None and row["s"] else None,
            "episode_n": int(episodes)}


def copy_stats_by_wallet(conn) -> dict:
    """Per-target paper-copy stats for lifecycle culling and fallback ranking."""
    from collections import defaultdict
    rows = conn.execute(
        "SELECT target, copy_tier, pnl_bps, exit_ts_ms FROM hl_copy_book "
        "WHERE status='closed' AND pnl_bps IS NOT NULL").fetchall()
    open_rows = conn.execute(
        "SELECT target, COUNT(*) n FROM hl_copy_book WHERE status='open' GROUP BY target").fetchall()
    open_counts = {r["target"]: int(r["n"]) for r in open_rows}
    agg = defaultdict(lambda: {"pnls": [], "wins": 0, "last_close_ts": 0,
                               "by_tier": defaultdict(lambda: {"pnls": [], "wins": 0})})
    for r in rows:
        a = agg[r["target"]]
        pnl = float(r["pnl_bps"])
        tier = r["copy_tier"] or "unknown"
        a["pnls"].append(pnl)
        if pnl > 0:
            a["wins"] += 1
            a["by_tier"][tier]["wins"] += 1
        a["by_tier"][tier]["pnls"].append(pnl)
        a["last_close_ts"] = max(a["last_close_ts"], int(r["exit_ts_ms"] or 0))
    out = {}
    for wallet, a in agg.items():
        n = len(a["pnls"])
        total = sum(a["pnls"])
        out[wallet] = {
            "n_closed": n,
            "mean_pnl_bps": round(total / n, 3) if n else 0.0,
            "sum_pnl_bps": round(total, 1),
            "wins": a["wins"],
            "win_rate": round(a["wins"] / n, 3) if n else 0.0,
            "open_n": open_counts.get(wallet, 0),
            "last_close_ts": a["last_close_ts"],
            "by_tier": {
                tier: {
                    "n_closed": len(bucket["pnls"]),
                    "mean_pnl_bps": round(sum(bucket["pnls"]) / len(bucket["pnls"]), 3)
                    if bucket["pnls"] else 0.0,
                    "sum_pnl_bps": round(sum(bucket["pnls"]), 1),
                    "wins": bucket["wins"],
                    "win_rate": round(bucket["wins"] / len(bucket["pnls"]), 3)
                    if bucket["pnls"] else 0.0,
                }
                for tier, bucket in a["by_tier"].items()
            },
        }
    for wallet, n_open in open_counts.items():
        out.setdefault(wallet, {
            "n_closed": 0,
            "mean_pnl_bps": 0.0,
            "sum_pnl_bps": 0.0,
            "wins": 0,
            "win_rate": 0.0,
            "open_n": n_open,
            "last_close_ts": 0,
            "by_tier": {},
        })
    return out


# ── tracked traders ───────────────────────────────────────────────────────────
def upsert_tracked(conn, wallet: str, info: dict, *, ts_ms: int) -> None:
    if "archetype_pass" in info:
        info = {**info, "archetype_pass": int(bool(info["archetype_pass"]))}
    cols = ("verdict", "archetype", "taker_frac", "profit_factor", "win_rate", "n_closes",
            "month_roi", "realized", "label", "archetype_pass")
    cur = conn.execute("SELECT wallet FROM hl_tracked_traders WHERE wallet=?", (wallet,)).fetchone()
    if cur:
        sets = ", ".join(f"{k}=?" for k in cols if k in info)
        vals = [info[k] for k in cols if k in info]
        if sets:
            conn.execute(f"UPDATE hl_tracked_traders SET {sets}, updated_ts_ms=? WHERE wallet=?",
                         (*vals, ts_ms, wallet))
    else:
        conn.execute(
            "INSERT INTO hl_tracked_traders(wallet, label, verdict, archetype, taker_frac, "
            "profit_factor, win_rate, n_closes, month_roi, realized, archetype_pass, added_ts_ms, updated_ts_ms, "
            "lifecycle_status, lifecycle_reason, lifecycle_updated_ts_ms, last_seen_ts_ms, culled_ts_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (wallet, info.get("label"), info.get("verdict"), info.get("archetype"),
             info.get("taker_frac"), info.get("profit_factor"), info.get("win_rate"),
             info.get("n_closes"), info.get("month_roi"), info.get("realized"),
             int(bool(info.get("archetype_pass"))), ts_ms, ts_ms,
             "active", "screen archetype-pass", ts_ms, ts_ms, None))
    if info.get("archetype_pass") in (1, True) or info.get("verdict") in ("watch", "copyable"):
        conn.execute(
            "UPDATE hl_tracked_traders SET lifecycle_status='active', lifecycle_reason=?, "
            "lifecycle_updated_ts_ms=?, last_seen_ts_ms=?, culled_ts_ms=NULL WHERE wallet=?",
            ("screen archetype-pass", ts_ms, ts_ms, wallet))
    conn.commit()


def set_tracked_lifecycle(conn, wallet: str, *, status: str, reason: str, ts_ms: int) -> None:
    culled_ts = ts_ms if status == "culled" else None
    conn.execute(
        "UPDATE hl_tracked_traders SET lifecycle_status=?, lifecycle_reason=?, "
        "lifecycle_updated_ts_ms=?, culled_ts_ms=? WHERE wallet=?",
        (status, reason, ts_ms, culled_ts, wallet))
    conn.commit()


def get_tracked(conn, *, include_culled: bool = False) -> list[dict]:
    q = "SELECT * FROM hl_tracked_traders"
    if not include_culled:
        q += " WHERE COALESCE(lifecycle_status, 'active') != 'culled'"
    return [dict(r) for r in conn.execute(q + " ORDER BY month_roi DESC").fetchall()]


# ── per-trader latest position snapshot ───────────────────────────────────────
def save_trader_state(conn, wallet: str, positions: dict, *, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO hl_trader_state(wallet, positions_json, ts_ms) VALUES (?,?,?) "
        "ON CONFLICT(wallet) DO UPDATE SET positions_json=excluded.positions_json, ts_ms=excluded.ts_ms",
        (wallet, json.dumps(positions), ts_ms))
    conn.commit()


def get_trader_state(conn, wallet: str) -> dict:
    row = conn.execute("SELECT positions_json FROM hl_trader_state WHERE wallet=?", (wallet,)).fetchone()
    return json.loads(row["positions_json"]) if row else {}


def seen_trader(conn, wallet: str) -> bool:
    """Have we ever snapshotted this trader? Distinguishes 'never seen' (baseline, don't follow
    their pre-existing book) from 'seen, currently flat' (a new position IS a real entry)."""
    return conn.execute("SELECT 1 FROM hl_trader_state WHERE wallet=?", (wallet,)).fetchone() is not None


# Copy-runner-specific baseline (separate from the follow runner's hl_trader_state so the two
# loops don't race on each other's 'last seen' state). Prevents cold-start mirroring: on first
# sight we record the target's pre-existing book and open NOTHING for it — only genuine entries
# detected while watching become mirrors.
def save_copy_trader_state(conn, wallet: str, positions: dict, *, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO hl_copy_trader_state(wallet, positions_json, ts_ms) VALUES (?,?,?) "
        "ON CONFLICT(wallet) DO UPDATE SET positions_json=excluded.positions_json, ts_ms=excluded.ts_ms",
        (wallet, json.dumps(positions), ts_ms))
    conn.commit()


def get_copy_trader_state(conn, wallet: str) -> dict:
    row = conn.execute("SELECT positions_json FROM hl_copy_trader_state WHERE wallet=?",
                       (wallet,)).fetchone()
    return json.loads(row["positions_json"]) if row else {}


def get_copy_trader_state_snapshot(conn, wallet: str) -> tuple[dict, int | None]:
    row = conn.execute(
        "SELECT positions_json,ts_ms FROM hl_copy_trader_state WHERE wallet=?", (wallet,)
    ).fetchone()
    if row is None:
        return {}, None
    return json.loads(row["positions_json"]), int(row["ts_ms"])


def seen_copy_trader(conn, wallet: str) -> bool:
    return conn.execute("SELECT 1 FROM hl_copy_trader_state WHERE wallet=?",
                        (wallet,)).fetchone() is not None


# ── follow positions ──────────────────────────────────────────────────────────
def open_follow(conn, f: dict, *, ts_ms: int) -> int:
    cur = conn.execute(
        "INSERT INTO hl_follow_positions(wallet, coin, side, kind, detect_ts_ms, entry_mark, status) "
        "VALUES (?,?,?,?,?,?, 'open')",
        (f["wallet"], f["coin"], f["side"], f.get("kind"), ts_ms, f.get("entry_mark")))
    conn.commit()
    return cur.lastrowid


def close_follow(conn, follow_id: int, *, exit_mark: float, net_bps: float, reason: str, ts_ms: int) -> None:
    conn.execute(
        "UPDATE hl_follow_positions SET status='closed', exit_ts_ms=?, exit_mark=?, net_bps=?, "
        "exit_reason=? WHERE id=?", (ts_ms, exit_mark, net_bps, reason, follow_id))
    conn.commit()


def get_open_follows(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_follow_positions WHERE status='open' ORDER BY detect_ts_ms DESC").fetchall()]


def recent_closed_follows(conn, limit: int = 30) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM hl_follow_positions WHERE status='closed' ORDER BY exit_ts_ms DESC LIMIT ?",
        (limit,)).fetchall()]


def follow_summary(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN net_bps>0 THEN 1 ELSE 0 END),0) wins, "
        "COALESCE(SUM(net_bps),0) s, COALESCE(AVG(net_bps),0) a "
        "FROM hl_follow_positions WHERE status='closed'").fetchone()
    open_n = conn.execute("SELECT COUNT(*) FROM hl_follow_positions WHERE status='open'").fetchone()[0]
    return {"closed": row["n"], "open": open_n, "wins": row["wins"],
            "win_rate": round(row["wins"] / row["n"], 3) if row["n"] else 0.0,
            "sum_net_bps": round(row["s"], 1), "avg_net_bps": round(row["a"], 2)}


def follow_stats_by_wallet(conn) -> dict:
    """Per-wallet closed-follow stats for the phase-2 gate. Clusters net_bps by coin×UTC-day."""
    from collections import defaultdict
    rows = conn.execute(
        "SELECT wallet, coin, net_bps, exit_ts_ms FROM hl_follow_positions "
        "WHERE status='closed' AND net_bps IS NOT NULL").fetchall()
    agg = defaultdict(lambda: {"nets": [], "by_coin_pnl": defaultdict(float),
                               "by_coin_cnt": defaultdict(int),
                               "by_cluster_sum": defaultdict(float),
                               "by_cluster_cnt": defaultdict(int), "last_close_ts": 0})
    for r in rows:
        a = agg[r["wallet"]]
        net = float(r["net_bps"])
        a["nets"].append(net)
        a["by_coin_pnl"][r["coin"]] += net
        a["by_coin_cnt"][r["coin"]] += 1
        ck = f"{r['coin']}|{int(r['exit_ts_ms']) // 86_400_000}"
        a["by_cluster_sum"][ck] += net
        a["by_cluster_cnt"][ck] += 1
        a["last_close_ts"] = max(a["last_close_ts"], int(r["exit_ts_ms"]))
    out = {}
    for w, a in agg.items():
        n = len(a["nets"])
        pos_total = sum(v for v in a["by_coin_pnl"].values() if v > 0) or 1e-9
        top_pnl = max((v for v in a["by_coin_pnl"].values()), default=0.0)
        top_cnt = max(a["by_coin_cnt"].values(), default=0)
        by_cluster = {k: a["by_cluster_sum"][k] / a["by_cluster_cnt"][k] for k in a["by_cluster_sum"]}
        out[w] = {
            "n_closed": n,
            "mean_net_bps": round(sum(a["nets"]) / n, 3) if n else 0.0,
            "top_asset_pnl_frac": round(max(0.0, top_pnl) / pos_total, 3),
            "top_asset_count_frac": round(top_cnt / n, 3) if n else 0.0,
            "last_close_ts": a["last_close_ts"],
            "by_cluster": by_cluster,
        }
    return out


# ── screen runs ───────────────────────────────────────────────────────────────
def save_screen_run(conn, run: dict, *, ts_ms: int) -> None:
    conn.execute(
        "INSERT INTO hl_screen_runs(ts_ms, leaderboard_n, prefiltered_n, screened_n, verdicts_json) "
        "VALUES (?,?,?,?,?)",
        (ts_ms, run.get("leaderboard_n"), run.get("prefiltered_n"), run.get("screened_n"),
         json.dumps(run.get("verdicts") or {})))
    conn.commit()


def latest_screen_run(conn) -> dict:
    row = conn.execute("SELECT * FROM hl_screen_runs ORDER BY ts_ms DESC LIMIT 1").fetchone()
    if not row:
        return {}
    d = dict(row)
    d["verdicts"] = json.loads(d.pop("verdicts_json") or "{}")
    return d

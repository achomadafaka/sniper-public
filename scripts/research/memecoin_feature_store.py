#!/usr/bin/env python3
"""
Memecoin signal feature-store builder.

Materializes `memecoin_signal_features` — one row per copy-signal — joining signal-time
features to forward-return labels already collected. Pure read-derived and rebuildable
(DROP + rebuild is safe; there is no incremental state). This is the substrate for the
feature→outcome scan (`memecoin_feature_scan.py`) and, later, the deploy-gated entry
conditioner.

Grain / joins (validated 2026-07-13 against live sniper_tracker.db + schema-architect review):
  - base grain   : memecoin_signal_outcomes.fomo_event_id (unique, 0 dupes)
  - features      : memecoin_events (100% 1:1 on fomo_event_id) — authoritative for
                    their_market_cap / their_size_usd / is_first_buy / chase_gap_pct / lag_s
  - curated flags : memecoin_firehose (~43% coverage) — was_curated / was_real_approved are
                    left NULL when no firehose row exists (never defaulted to 0); a separate
                    `firehose_captured` boolean records observation.
  - labels        : memecoin_signal_outcomes.max_gain_pct / peak_horizon_s / n_marks / status
  - copy outcome  : nearest memecoin_positions row filtered to
                    strategy='real_shadow' AND execution_mode='paper' (the live real rails'
                    paper mirror) on (mint, source_user_id) within ±HOLD_MATCH_S of buy_ts.
                    The strategy/mode filter is REQUIRED — origin_event_id fans out 2-5x
                    across arms otherwise.

Label honesty (leak-free): max_gain_pct is a running maximum, so
  hit(threshold) = 1  if max_gain_pct >= threshold          (monotonic -> final, even if open)
                 = 0  if status='matured' and below          (resolved, did not hit)
                 = NULL otherwise                            (still open & below, or no data)
mcap_band / size_band are finer buckets that roll up to the established
sub-$1M / $1-30M / >$30M convention (finding-memecoin-signal-conditioning-2026-07-11).

Usage:
    python3 scripts/research/memecoin_feature_store.py --db /path/sniper_tracker.db [--rebuild]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Allow `python3 scripts/research/memecoin_feature_store.py` (CLI) to import the sniper package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Canonical band functions live in the runtime module so the store and the entry conditioner
# bucket identically (a mismatch would make the frozen conditioner constants wrong). Re-exported
# here for callers/tests that import from the feature store.
from sniper.services.memecoin_entry_conditioner import mcap_band, size_band  # noqa: E402,F401

HOLD_MATCH_S = 600  # ±10 min window to match a signal to its real_shadow twin position
# A forward gain above this is a quote glitch, not a real move (2026-07-13 methodology audit found a
# single bad_quote row at max_gain=491,552% that polluted every AVG(max_gain_pct)). Such rows — and
# any status='bad_quote' row — are treated as UNLABELED (label + max_gain NULLed), never averaged.
MAX_GAIN_SANITY_PCT = 2000.0

FEATURE_COLUMNS = [
    "fomo_event_id", "source_user_id", "handle", "mint", "ticker", "network_id",
    "buy_ts", "ref_price", "their_market_cap", "their_size_usd", "is_first_buy",
    "chase_gap_pct", "lag_s", "was_curated", "was_real_approved", "firehose_captured",
    "consensus_60m", "mcap_band", "size_band", "max_gain_pct", "peak_horizon_s",
    "n_marks", "hit20", "hit50", "shadow_realized_pnl", "shadow_exit_reason",
    "label_status", "label_source", "built_at",
]


def _hit(threshold: float, max_gain: float | None, status: str | None) -> int | None:
    if max_gain is None:
        return None
    if max_gain >= threshold:
        return 1  # monotonic running-max: once hit, final
    if status == "matured":
        return 0  # resolved below threshold
    return None   # still open and below -> could still hit


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_signal_features(
            fomo_event_id      TEXT PRIMARY KEY,
            source_user_id     TEXT,
            handle             TEXT,
            mint               TEXT,
            ticker             TEXT,
            network_id         TEXT,
            buy_ts             TEXT,
            ref_price          REAL,
            their_market_cap   REAL,
            their_size_usd     REAL,
            is_first_buy       INTEGER,
            chase_gap_pct      REAL,
            lag_s              REAL,
            was_curated        INTEGER,   -- NULL when no firehose row (not 0)
            was_real_approved  INTEGER,   -- NULL when no firehose row
            firehose_captured  INTEGER,   -- 1 if a firehose row exists, else 0
            consensus_60m      INTEGER,
            mcap_band          TEXT,
            size_band          TEXT,
            max_gain_pct       REAL,
            peak_horizon_s     REAL,
            n_marks            INTEGER,
            hit20              INTEGER,    -- NULL until resolvable (leak-free)
            hit50              INTEGER,
            shadow_realized_pnl REAL,     -- gross paper PnL of the real_shadow twin (nullable)
            shadow_exit_reason TEXT,
            label_status       TEXT,      -- mirrors signal_outcomes.status
            label_source       TEXT,      -- 'v1' | 'v2': WHICH capture produced the label.
                                          -- Never mix them in an aggregate without grouping on it.
            built_at           TEXT       -- build timestamp, NOT event capture time
        )
        """
    )
    # CREATE TABLE IF NOT EXISTS never alters a table that already exists, so a database built by
    # an earlier version keeps the old 28 columns and the INSERT below dies with
    # "no such column: label_source". Add it in place, idempotently.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_signal_features)")}
    if "label_source" not in existing:
        conn.execute("ALTER TABLE memecoin_signal_features ADD COLUMN label_source TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feat_mcap_band ON memecoin_signal_features(mcap_band)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feat_size_band ON memecoin_signal_features(size_band)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feat_network ON memecoin_signal_features(network_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feat_mint ON memecoin_signal_features(mint)")


def _v2_labels(conn: sqlite3.Connection) -> dict:
    """Forward-return labels rebuilt from the v2 capture, keyed by upstream_event_id.

    v1 stopped writing marks for chain 4663 entirely — 64,528 rows, zero marks, and 59,522 of them
    matured anyway — so `max_gain_pct` was populated on 99% of solana rows and 0% of 4663 rows.
    Every band and hit-rate analysis therefore ran on solana alone while the gate under test fired
    87% on 4663. v2 captured 4663 the whole time: 359,196 horizon marks over 59,872 outcomes.

    Verified before this was written: v2.upstream_event_id joins v1.fomo_event_id 1:1 with no
    duplicates; v2.ref_price is present on 100% of rows on both chains; and recomputing v1's solana
    rows from v2 reproduces them exactly. This is the same metric from a capture that did not stop.

    `resolved` requires EVERY horizon observed. A missing mark could have hidden the peak, so a
    partial set may prove a hit and may never prove a miss.
    """
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('memecoin_signal_outcomes_v2','memecoin_horizon_marks_v2')")}
    if len(have) < 2:
        return {}                      # older DB / fixture without the v2 capture
    rows = conn.execute(
        """
        SELECT ev, max_gain, target_s AS peak_horizon_s, n_obs, n_total FROM (
          SELECT v.upstream_event_id AS ev,
                 h.target_s,
                 MAX((h.price / v.ref_price - 1.0) * 100.0)
                     OVER (PARTITION BY v.upstream_event_id) AS max_gain,
                 SUM(h.price IS NOT NULL)
                     OVER (PARTITION BY v.upstream_event_id) AS n_obs,
                 COUNT(*) OVER (PARTITION BY v.upstream_event_id) AS n_total,
                 ROW_NUMBER() OVER (PARTITION BY v.upstream_event_id
                                    ORDER BY h.price DESC) AS rn
            FROM memecoin_signal_outcomes_v2 v
            JOIN memecoin_horizon_marks_v2 h ON h.outcome_id = v.outcome_id
           WHERE v.ref_price IS NOT NULL AND v.ref_price > 0
        ) WHERE rn = 1
        """
    ).fetchall()
    out = {}
    for r in rows:
        if r["max_gain"] is None:
            continue                   # nothing observed yet -> no label, not a zero
        out[r["ev"]] = {
            "max_gain_pct": r["max_gain"],
            "peak_horizon_s": r["peak_horizon_s"],
            "n_marks": int(r["n_obs"] or 0),
            "status": "matured" if r["n_obs"] == r["n_total"] else "open",
        }
    return out


def _shadow_twin(conn: sqlite3.Connection, mint: str, uid: str, buy_ts: str):
    """Nearest real_shadow/paper position on (mint, source_user_id) within HOLD_MATCH_S of buy_ts.
    The strategy+mode filter prevents cross-arm origin_event_id fan-out."""
    if not (mint and uid and buy_ts):
        return (None, None)
    row = conn.execute(
        """SELECT realized_pnl, exit_reason,
                  ABS(strftime('%s',opened_at) - strftime('%s',?)) dt
             FROM memecoin_positions
            WHERE mint=? AND source_user_id=? AND strategy='real_shadow'
                  AND execution_mode='paper' AND opened_at IS NOT NULL
            ORDER BY dt LIMIT 1""",
        (buy_ts, mint, uid),
    ).fetchone()
    if row is None or row["dt"] is None or row["dt"] > HOLD_MATCH_S:
        return (None, None)
    return (row["realized_pnl"], row["exit_reason"])


def build_feature_store(conn: sqlite3.Connection) -> int:
    """(Re)build memecoin_signal_features from source tables. Idempotent. Returns row count."""
    conn.row_factory = sqlite3.Row
    _ensure_table(conn)

    base = conn.execute(
        """
        SELECT o.fomo_event_id, o.source_user_id, o.handle, o.mint, o.ticker, o.buy_ts,
               o.ref_price, o.max_gain_pct, o.peak_horizon_s, o.n_marks, o.status,
               o.network_id AS o_net,
               e.their_market_cap, e.their_size_usd, e.is_first_buy, e.chase_gap_pct,
               e.lag_s, e.network_id AS e_net,
               f.was_curated, f.was_real_approved,
               (f.fomo_event_id IS NOT NULL) AS firehose_captured
          FROM memecoin_signal_outcomes o
          LEFT JOIN memecoin_events e   ON o.fomo_event_id = e.fomo_event_id
          LEFT JOIN memecoin_firehose f ON o.fomo_event_id = f.fomo_event_id
        """
    ).fetchall()

    v2_labels = _v2_labels(conn)
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in base:
        mint, uid, buy_ts = r["mint"], r["source_user_id"], r["buy_ts"]
        # consensus: distinct prior buyers on the same mint within 60 min BEFORE this signal
        consensus = conn.execute(
            """SELECT COUNT(DISTINCT source_user_id)
                 FROM memecoin_signal_outcomes
                WHERE mint=? AND buy_ts < ?
                      AND strftime('%s',?) - strftime('%s',buy_ts) <= 3600
                      AND source_user_id != ?""",
            (mint, buy_ts, buy_ts, uid),
        ).fetchone()[0]
        shadow_pnl, shadow_exit = _shadow_twin(conn, mint, uid, buy_ts)
        mg, status = r["max_gain_pct"], r["status"]
        peak_h, n_marks, source = r["peak_horizon_s"], r["n_marks"], "v1"
        # v1 wins wherever it actually observed something, so no existing number moves. It is the
        # rows v1 left EMPTY that v2 fills -- which on 4663 is all of them.
        if not r["n_marks"]:
            v2 = v2_labels.get(r["fomo_event_id"])
            if v2 is not None:
                mg, status = v2["max_gain_pct"], v2["status"]
                peak_h, n_marks, source = v2["peak_horizon_s"], v2["n_marks"], "v2"
        # Corrupt outcome (bad_quote status or a glitch-level gain) -> treat as unlabeled: NULL the
        # gain AND the hit flags, so it never pollutes an aggregate or reads as a real hit/miss.
        if status == "bad_quote" or (mg is not None and mg > MAX_GAIN_SANITY_PCT):
            mg = None
        net = r["e_net"] or r["o_net"]
        rows.append((
            r["fomo_event_id"], uid, r["handle"], mint, r["ticker"], net, buy_ts,
            r["ref_price"], r["their_market_cap"], r["their_size_usd"], r["is_first_buy"],
            r["chase_gap_pct"], r["lag_s"], r["was_curated"], r["was_real_approved"],
            1 if r["firehose_captured"] else 0, consensus,
            mcap_band(r["their_market_cap"]), size_band(r["their_size_usd"]),
            mg, peak_h, n_marks,
            _hit(20, mg, status), _hit(50, mg, status),
            shadow_pnl, shadow_exit, status, source, now_iso,
        ))

    placeholders = ",".join("?" * len(FEATURE_COLUMNS))
    conn.execute("DELETE FROM memecoin_signal_features")
    conn.executemany(
        f"INSERT INTO memecoin_signal_features({','.join(FEATURE_COLUMNS)}) VALUES({placeholders})",
        rows,
    )
    conn.commit()
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="sniper_tracker.db")
    ap.add_argument("--rebuild", action="store_true", help="(default behaviour; kept for clarity)")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=30)
    n = build_feature_store(conn)
    labeled = conn.execute(
        "SELECT COUNT(*) FROM memecoin_signal_features WHERE hit20 IS NOT NULL").fetchone()[0]
    print(f"built memecoin_signal_features: {n} rows ({labeled} with a resolvable hit20 label)")


if __name__ == "__main__":
    main()

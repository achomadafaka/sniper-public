"""Structured, queryable analysis store for the winning-wallet thesis extractor.

PER-WALLET isolation: each wallet gets its OWN SQLite file at
`runs/wallet_analysis/<wallet>.db` with its own tables — wallets NEVER share a table, so
there is zero overlap. Matches the rest of the analyzer (activity + profile are per-wallet
files) and the one-wallet-at-a-time doctrine. The dir is isolated from the operational DB so
a data wipe doesn't touch it. The flat JSON profile (store.py) stays for the report view;
this is the queryable layer beneath it.

Each wallet DB has: wa_runs, wa_activity (raw events), wa_positions (reconstructed episodes),
wa_slices ((r-p) edge slices), wa_clusters, wa_findings. Multiple runs of the same wallet
live in that one file, distinguished by run_id with an is_current flag for the latest."""
from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import time
from typing import Any

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "runs", "wallet_analysis")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_runs (
  run_id TEXT PRIMARY KEY, wallet TEXT NOT NULL, created_ts INTEGER,
  headline TEXT, total_realized_pnl REAL, reward_share_pct REAL,
  n_positions INTEGER, n_edge_clusters INTEGER, n_loss_centers INTEGER,
  inventory_reconciliation REAL, is_current INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS wa_activity (
  run_id TEXT, seq INTEGER, kind TEXT, condition_id TEXT,
  outcome_index INTEGER, episode_index INTEGER, outcome TEXT, ts INTEGER, price REAL, size REAL,
  usdc REAL, tx TEXT, event_slug TEXT, title TEXT
);
CREATE TABLE IF NOT EXISTS wa_positions (
  run_id TEXT, condition_id TEXT, outcome_index INTEGER, episode_index INTEGER,
  event_slug TEXT, title TEXT, outcome TEXT, category TEXT, subcat TEXT, template TEXT, tags TEXT,
  buy_shares REAL, buy_cost REAL, sell_shares REAL, sell_proceeds REAL,
  split_shares REAL, split_cost REAL, merge_shares REAL, merge_cash REAL,
  convert_in_shares REAL, convert_in_cost REAL, convert_out_shares REAL,
  convert_out_cost REAL, convert_cash REAL,    -- negRisk CONVERSION flow (else PnL rows look unexplained)
  entry_vwap REAL, first_buy_ts INTEGER, last_ts INTEGER, resolution_ts INTEGER,
  resolved INTEGER, won INTEGER, is_push INTEGER, is_directional INTEGER,
  exit_mechanism TEXT, realized_pnl REAL,
  current_price REAL, current_value REAL, unrealized_pnl REAL    -- open-position marks as of analysis time
);
CREATE TABLE IF NOT EXISTS wa_slices (
  run_id TEXT, axis TEXT, slice_key TEXT, n_positions INTEGER, n_events INTEGER,
  wins INTEGER, win_rate REAL, mean_entry REAL, edge_pp REAL, pnl REAL, roi_pct REAL
);
CREATE TABLE IF NOT EXISTS wa_clusters (
  run_id TEXT, cluster_id TEXT, label TEXT, axis TEXT,
  n_positions INTEGER, n_events INTEGER, pnl REAL, edge_pp REAL, is_loss_center INTEGER
);
CREATE TABLE IF NOT EXISTS wa_findings (
  run_id TEXT, cluster_id TEXT, method TEXT, state TEXT,
  effect REAL, n INTEGER, n_events INTEGER, runnability TEXT
);
CREATE INDEX IF NOT EXISTS idx_wa_pos_run ON wa_positions(run_id);
CREATE INDEX IF NOT EXISTS idx_wa_pos_cond ON wa_positions(condition_id);
CREATE INDEX IF NOT EXISTS idx_wa_act_run ON wa_activity(run_id);
CREATE INDEX IF NOT EXISTS idx_wa_slices_run ON wa_slices(run_id, axis);
"""

_WALLET_RE = re.compile(r"^0x[a-f0-9]{40}$")


def db_path(wallet: str) -> str:
    """Per-wallet DB path. Validates the address here (the single chokepoint every read/write
    goes through) so a CLI or direct caller can't create an arbitrary file under DB_DIR via a
    crafted `wallet` (path traversal / junk filenames)."""
    w = (wallet or "").lower()
    if not _WALLET_RE.match(w):
        raise ValueError(f"invalid wallet address for db_path: {wallet!r}")
    return os.path.join(DB_DIR, f"{w}.db")


# columns added after the first ship — ALTERed into existing per-wallet DBs on connect()
_WA_POS_ADDED = (("outcome", "TEXT"), ("current_price", "REAL"),
                 ("current_value", "REAL"), ("unrealized_pnl", "REAL"),
                 # negRisk CONVERSION flow — present on PositionEpisode but previously unpersisted,
                 # so conversion-driven PnL rows (e.g. 0x5bff...) had no buy/sell/split/merge story
                 # in the UI. (review #MED 2026-06-17)
                 ("convert_in_shares", "REAL"), ("convert_in_cost", "REAL"),
                 ("convert_out_shares", "REAL"), ("convert_out_cost", "REAL"),
                 ("convert_cash", "REAL"))


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(wa_positions)")}
    for name, decl in _WA_POS_ADDED:
        if name not in have:
            conn.execute(f"ALTER TABLE wa_positions ADD COLUMN {name} {decl}")


def connect(wallet: str) -> sqlite3.Connection:
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(db_path(wallet), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _g(obj: Any, name: str, default=None):
    return getattr(obj, name, default)


def _held_shares(e) -> float:
    # Use the episode's canonical held_shares (includes negRisk convert flow); fall back to the
    # share-arithmetic for any object lacking the property. (review-2026-06-17: convert terms were
    # dropped here, mislabeling conversion-held positions as closed.)
    held = getattr(e, "held_shares", None)
    if held is not None:
        return held
    return ((e.buy_shares or 0.0) + (e.split_shares or 0.0) + getattr(e, "convert_in_shares", 0.0)
            - (e.sell_shares or 0.0) - (e.merge_shares or 0.0) - getattr(e, "convert_out_shares", 0.0))


def persist_run(*, profile, book, slice_tables, clusters, ledger, current_marks=None) -> str:
    """Materialize one analysis run into this wallet's OWN DB file. Marks prior runs in that
    file is_current=0; this run is_current=1. Returns the run_id.

    `current_marks` (optional): {(condition_id, outcome_index): {current_price, current_value,
    unrealized_pnl}} from the live /positions snapshot at analysis time — attached to still-open
    episodes so the UI can show real-time PnL for active positions."""
    marks = current_marks or {}
    wallet = profile.wallet.lower()
    run_id = profile.run_id
    cr = _g(profile, "capital_risk")
    total = (_g(cr, "total_realized_pnl_with_inventory") if cr else None)
    if total is None and cr is not None:
        total = _g(cr, "total_realized_pnl")
    inv_rec = _g(cr, "inventory_reconciliation") if cr else None

    conn = connect(wallet)
    try:
        with conn:                                  # single transaction
            conn.execute("UPDATE wa_runs SET is_current=0")
            for t in ("wa_runs", "wa_activity", "wa_positions", "wa_slices", "wa_clusters", "wa_findings"):
                conn.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))   # idempotent re-run

            conn.execute(
                """INSERT INTO wa_runs(run_id, wallet, created_ts, headline, total_realized_pnl,
                   reward_share_pct, n_positions, n_edge_clusters, n_loss_centers,
                   inventory_reconciliation, is_current)
                   VALUES(?,?,?,?,?,?,?,?,?,?,1)""",
                (run_id, wallet, int(time.time()), profile.headline, total,
                 _g(profile, "reward_share_pct"), len(book.episodes),
                 len(_g(profile, "edge_clusters", [])), len(_g(profile, "loss_centers", [])), inv_rec))

            conn.executemany(
                """INSERT INTO wa_activity(run_id, seq, kind, condition_id, outcome_index, episode_index,
                   outcome, ts, price, size, usdc, tx, event_slug, title)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, r.seq, r.kind, r.condition_id, r.outcome_index, r.episode_index, r.outcome,
                  r.ts, r.price, r.size, r.usdc, r.tx, r.event_slug, r.title) for r in ledger.rows])

            def _pos_row(e):
                # attach the live mark only to a still-open episode (resolved ones carry realized_pnl)
                mk = marks.get((e.condition_id, e.outcome_index)) if not e.resolved and _held_shares(e) > 1e-6 else None
                return (run_id, e.condition_id, e.outcome_index, e.episode_index, e.event_slug,
                        e.title, e.outcome, e.category, e.subcat, e.template, json.dumps(e.tags or []),
                        e.buy_shares, e.buy_cost, e.sell_shares, e.sell_proceeds, e.split_shares,
                        e.split_cost, e.merge_shares, e.merge_cash,
                        _g(e, "convert_in_shares", 0.0), _g(e, "convert_in_cost", 0.0),
                        _g(e, "convert_out_shares", 0.0), _g(e, "convert_out_cost", 0.0),
                        _g(e, "convert_cash", 0.0), e.entry_vwap, e.first_buy_ts,
                        e.last_ts, e.resolution_ts, int(e.resolved), e.won, int(e.is_push),
                        int(e.is_directional), e.exit_mechanism, e.realized_pnl,
                        (mk or {}).get("current_price"), (mk or {}).get("current_value"),
                        (mk or {}).get("unrealized_pnl"))

            conn.executemany(
                """INSERT INTO wa_positions(run_id, condition_id, outcome_index, episode_index,
                   event_slug, title, outcome, category, subcat, template, tags, buy_shares, buy_cost,
                   sell_shares, sell_proceeds, split_shares, split_cost, merge_shares, merge_cash,
                   convert_in_shares, convert_in_cost, convert_out_shares, convert_out_cost, convert_cash,
                   entry_vwap, first_buy_ts, last_ts, resolution_ts, resolved, won, is_push,
                   is_directional, exit_mechanism, realized_pnl, current_price, current_value, unrealized_pnl)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [_pos_row(e) for e in book.episodes])

            slice_rows = []
            for axis, table in (slice_tables or {}).items():
                for key, s in table.items():
                    slice_rows.append((run_id, axis, key, s.n_positions, s.n_events,
                                       s.wins, s.win_rate, s.mean_entry, s.edge_pp, s.pnl, s.roi_pct))
            conn.executemany(
                """INSERT INTO wa_slices(run_id, axis, slice_key, n_positions, n_events,
                   wins, win_rate, mean_entry, edge_pp, pnl, roi_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                slice_rows)

            conn.executemany(
                """INSERT INTO wa_clusters(run_id, cluster_id, label, axis, n_positions,
                   n_events, pnl, edge_pp, is_loss_center) VALUES(?,?,?,?,?,?,?,?,?)""",
                [(run_id, c.cluster_id, c.label, c.axis, c.n_positions, c.n_events,
                  c.pnl, c.edge_pp, int(c.is_loss_center)) for c in clusters])

            finding_rows = []
            for cluster_id, findings in (_g(profile, "method_findings", {}) or {}).items():
                for f in findings:
                    finding_rows.append((run_id, cluster_id, _g(f, "method"), _g(f, "state"),
                                         _g(f, "effect"), _g(f, "n"), _g(f, "n_events"),
                                         _g(f, "runnability")))
            conn.executemany(
                """INSERT INTO wa_findings(run_id, cluster_id, method, state, effect, n,
                   n_events, runnability) VALUES(?,?,?,?,?,?,?,?)""", finding_rows)
        return run_id
    finally:
        conn.close()


# ── read helpers (used by the API) — each opens that wallet's own file ──────
def _current_run_id(conn) -> str | None:
    row = conn.execute("SELECT run_id FROM wa_runs WHERE is_current=1").fetchone()
    return row["run_id"] if row else None


def has_store(wallet: str) -> bool:
    try:
        return os.path.exists(db_path(wallet))
    except ValueError:
        return False


def list_wallets() -> list[dict]:
    """Scan the per-wallet DB dir and return each wallet's current-run summary."""
    out = []
    for path in sorted(glob.glob(os.path.join(DB_DIR, "*.db"))):
        try:
            conn = sqlite3.connect(path, timeout=10)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM wa_runs WHERE is_current=1").fetchone()
            if row:
                out.append(dict(row))
            conn.close()
        except Exception:
            continue
    out.sort(key=lambda r: r.get("created_ts") or 0, reverse=True)
    return out


def positions_for(wallet: str) -> list[dict]:
    if not has_store(wallet):
        return []
    conn = connect(wallet)
    try:
        rid = _current_run_id(conn)
        if not rid:
            return []
        rows = conn.execute(
            "SELECT * FROM wa_positions WHERE run_id=? ORDER BY ABS(COALESCE(realized_pnl,0)) DESC", (rid,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def analysis_state(wallet: str) -> dict:
    """Current analyzer run cursor for a wallet.

    This is read-only groundwork for incremental refresh: callers can see the current
    run timestamp and the newest activity timestamp without touching analyzer math.
    """
    w = (wallet or "").lower()
    base = {
        "wallet": w,
        "has_store": False,
        "run_id": None,
        "created_ts": None,
        "max_activity_ts": None,
        "n_activity": 0,
        "n_positions": 0,
    }
    if not has_store(wallet):
        return base
    conn = connect(wallet)
    try:
        rid = _current_run_id(conn)
        if not rid:
            return {**base, "has_store": True}
        run = conn.execute("SELECT run_id, created_ts FROM wa_runs WHERE run_id=?", (rid,)).fetchone()
        act = conn.execute(
            "SELECT COUNT(*) AS n_activity, MAX(ts) AS max_activity_ts FROM wa_activity WHERE run_id=?",
            (rid,),
        ).fetchone()
        pos = conn.execute(
            "SELECT COUNT(*) AS n_positions FROM wa_positions WHERE run_id=?",
            (rid,),
        ).fetchone()
        return {
            "wallet": w,
            "has_store": True,
            "run_id": run["run_id"] if run else rid,
            "created_ts": run["created_ts"] if run else None,
            "max_activity_ts": act["max_activity_ts"] if act else None,
            "n_activity": act["n_activity"] if act else 0,
            "n_positions": pos["n_positions"] if pos else 0,
        }
    finally:
        conn.close()


def activity_for_position(wallet: str, condition_id: str, outcome_index: int,
                          episode_index: int) -> list[dict]:
    """Full activity for one reconstructed position: its directional trades (matching
    condition+outcome+episode) PLUS the condition-level events (split/merge/redeem, which have
    no single outcome and are shared across the pair). Chronological."""
    if not has_store(wallet):
        return []
    conn = connect(wallet)
    try:
        rid = _current_run_id(conn)
        if not rid:
            return []
        rows = conn.execute(
            """SELECT kind, outcome_index, episode_index, outcome, ts, price, size, usdc, tx, title
               FROM wa_activity
               WHERE run_id=? AND condition_id=?
                 AND ( (kind IN ('buy','sell') AND outcome_index=? AND episode_index=?)
                       OR kind IN ('split','merge','redeem') )
               ORDER BY ts, seq""",
            (rid, condition_id, outcome_index, episode_index)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def slices_for(wallet: str, *, axis: str | None = None) -> list[dict]:
    if not has_store(wallet):
        return []
    conn = connect(wallet)
    try:
        rid = _current_run_id(conn)
        if not rid:
            return []
        if axis:
            rows = conn.execute("SELECT * FROM wa_slices WHERE run_id=? AND axis=? ORDER BY pnl DESC", (rid, axis)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM wa_slices WHERE run_id=? ORDER BY axis, pnl DESC", (rid,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

#!/usr/bin/env python3
"""HL alt run-up short — observational paper ledger with real order-book fills.

The frozen candidate, unchanged and untunable at runtime:

    nominate  runup_72h = close/min(close[t-72:t]) - 1 crosses up through +30%
    skip      funding_self_pct > 0.94993  (top-5% of the coin's own 30d funding)
    enter     SHORT at the first executable book price after the nomination hour
    exit      first of: -15% take-profit, +20% stop, 24h cap
    naked     no alt-basket leg; the hedged residual is a research statistic, not a trade

Why this exists: the backtest fills exits at the STOP PRICE off hourly candles. That is the one
assumption a backtest cannot check on itself. Here every entry and exit is priced by walking the
live L2 book, so ~25 closes are enough to say whether the modelled fills were real.

Every fill is priced at BOTH the operator clip and a $5,000 reference from the same book snapshot.
A small clip crosses one or two levels and flatters the result; recording both makes the cost curve
visible instead of trusting the provider's fixed impact probe.

Arm identity is the POLICY hash plus taxonomy/benchmark/regime versions — never code_sha. Five arms
in 36 hours on an unchanged policy is why nothing in this program has ever reached a gate.

NO KEYS. NO ORDERS. NO FUNDED PATH. Read-only public endpoints; writes only its own database.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.markets.hyperliquid_api import (  # noqa: E402
    _post_info, fetch_funding_history, fetch_l2_book,
)
from sniper.hyperlive import funding as FUND  # noqa: E402
from sniper.services.memecoin_runner import acquire_run_lock

logger = logging.getLogger("hl_altshort_paper")

HOUR_MS = 3_600_000
MAJORS = {"BTC", "ETH", "SOL"}
REFERENCE_NOTIONAL = 5_000.0
TAKER_FEE_BPS_ROUND_TRIP = 9.0
FUNDING_WINDOW_H = 720                    # 30 days for the self-percentile

POLICY = {
    "runup_pct": 0.30, "runup_lookback_h": 72,
    "take_profit_mult": 0.85, "stop_loss_mult": 1.20, "cap_hours": 24,
    "funding_self_pct_max": 0.94993,
    "side": "short", "hedge": "none",
    "taker_fee_bps_round_trip": TAKER_FEE_BPS_ROUND_TRIP,
}
TAXONOMY_VERSION = "hl-alt-exclude-btc-eth-sol-v1"
BENCHMARK_VERSION = "naked-no-basket-v1"
REGIME_VERSION = "trailing-alt-basket-72h-observation-v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hl_paper_arms (
    arm_id TEXT PRIMARY KEY, policy_hash TEXT NOT NULL, policy_json TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL, benchmark_version TEXT NOT NULL,
    regime_version TEXT NOT NULL, clip_usd REAL NOT NULL, max_concurrent INTEGER NOT NULL,
    created_ts_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_paper_observations (
    obs_id TEXT PRIMARY KEY, arm_id TEXT NOT NULL, coin TEXT NOT NULL,
    decision_ts_ms INTEGER NOT NULL, decision_close REAL NOT NULL, runup_72h REAL NOT NULL,
    funding_24h_bps REAL, funding_self_pct REAL, gated_in INTEGER NOT NULL,
    gate_reason TEXT NOT NULL, created_ts_ms INTEGER NOT NULL,
    portfolio_slot_eligible INTEGER NOT NULL DEFAULT 1,
    entry_status TEXT NOT NULL DEFAULT 'not_applicable', entry_reason TEXT,
    episode_id TEXT, episode_primary INTEGER,
    trough_ts_ms INTEGER, trough_px REAL,
    regime_label TEXT, regime_basket_bps REAL, regime_coverage REAL,
    UNIQUE (arm_id, coin, decision_ts_ms)
);
CREATE TABLE IF NOT EXISTS hl_paper_regimes (
    arm_id TEXT NOT NULL, decision_ts_ms INTEGER NOT NULL, label TEXT NOT NULL,
    basket_return_bps REAL, coverage REAL NOT NULL, eligible_n INTEGER NOT NULL,
    covered_n INTEGER NOT NULL, created_ts_ms INTEGER NOT NULL,
    PRIMARY KEY (arm_id, decision_ts_ms)
);
CREATE TABLE IF NOT EXISTS hl_paper_positions (
    position_id TEXT PRIMARY KEY, arm_id TEXT NOT NULL, obs_id TEXT NOT NULL,
    coin TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('open','closed')),
    clip_usd REAL NOT NULL,
    entry_ts_ms INTEGER NOT NULL, entry_px_clip REAL NOT NULL, entry_qty_clip REAL NOT NULL,
    entry_px_ref REAL, entry_qty_ref REAL, entry_mid REAL, entry_spread_bps REAL,
    entry_levels_clip INTEGER, entry_levels_ref INTEGER,
    exit_ts_ms INTEGER, exit_px_clip REAL, exit_px_ref REAL, exit_reason TEXT,
    exit_mid REAL, exit_spread_bps REAL, exit_levels_clip INTEGER, exit_levels_ref INTEGER,
    stop_px REAL NOT NULL, target_px REAL NOT NULL, deadline_ts_ms INTEGER NOT NULL,
    funding_bps REAL, funding_pnl_bps REAL, funding_status TEXT,
    funding_provenance_status TEXT,
    gross_bps_clip REAL, gross_bps_ref REAL,
    net_bps_clip REAL, net_bps_ref REAL,
    modelled_gross_bps REAL, fill_delta_bps REAL,
    UNIQUE (arm_id, obs_id)
);
CREATE TABLE IF NOT EXISTS hl_paper_marks (
    position_id TEXT NOT NULL, ts_ms INTEGER NOT NULL, mid REAL, book_px_clip REAL,
    spread_bps REAL, PRIMARY KEY (position_id, ts_ms)
);
CREATE INDEX IF NOT EXISTS idx_paper_pos_open ON hl_paper_positions(status, arm_id);
PRAGMA user_version=3;
"""


def arm_semantics(clip_usd=40.0, max_concurrent=5) -> dict:
    return {
        "policy": POLICY, "taxonomy": TAXONOMY_VERSION,
        "benchmark": BENCHMARK_VERSION, "regime": REGIME_VERSION,
        "clip_usd": float(clip_usd), "reference_notional": REFERENCE_NOTIONAL,
        "max_concurrent": int(max_concurrent),
        "entry_clock": "first-scan-in-hour-after-completed-nomination-v1",
        "exit_quantity": "exact-entry-quantity-v1",
        "repeat_reset": "causal-72h-trough-identity-v1",
    }


def policy_hash(clip_usd=40.0, max_concurrent=5) -> str:
    return hashlib.sha256(json.dumps(
        arm_semantics(clip_usd, max_concurrent), sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def arm_id(clip_usd=40.0, max_concurrent=5) -> str:
    """Identity is policy semantics. A code change alone must NOT mint a new arm."""
    return "altshort-naked-" + policy_hash(clip_usd, max_concurrent)[:16]


def _add_missing_columns(conn, table, columns):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate(conn):
    """Additive migrations preserve prior arms and their evidence."""
    _add_missing_columns(conn, "hl_paper_observations", {
        "portfolio_slot_eligible": "INTEGER NOT NULL DEFAULT 1",
        "entry_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
        "entry_reason": "TEXT",
        "episode_id": "TEXT",
        "episode_primary": "INTEGER",
        "trough_ts_ms": "INTEGER",
        "trough_px": "REAL",
        "regime_label": "TEXT", "regime_basket_bps": "REAL", "regime_coverage": "REAL",
    })
    _add_missing_columns(conn, "hl_paper_positions", {
        "entry_qty_clip": "REAL", "entry_qty_ref": "REAL",
        "entry_levels_clip": "INTEGER", "entry_levels_ref": "INTEGER",
        "exit_mid": "REAL", "exit_spread_bps": "REAL",
        "exit_levels_clip": "INTEGER", "exit_levels_ref": "INTEGER",
        "funding_status": "TEXT",
        "funding_pnl_bps": "REAL",
        "funding_provenance_status": "TEXT",
    })
    _add_missing_columns(conn, "hl_paper_regimes", {
        "fetch_error_n": "INTEGER NOT NULL DEFAULT 0",
        "latest_mismatch_n": "INTEGER NOT NULL DEFAULT 0",
    })
    episode_info = {
        row[1]: row for row in conn.execute("PRAGMA table_info(hl_paper_observations)")
    }
    if not episode_info["episode_primary"][3]:
        conn.execute(
            "UPDATE hl_paper_observations SET episode_primary=NULL WHERE episode_id IS NULL"
        )
    conn.execute(
        "UPDATE hl_paper_positions SET funding_provenance_status=funding_status "
        "WHERE funding_provenance_status IS NULL AND funding_status IS NOT NULL"
    )
    conn.execute("PRAGMA user_version=5")


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def ensure_arm(conn, clip_usd, max_concurrent, now_ms):
    aid, phash = arm_id(clip_usd, max_concurrent), policy_hash(clip_usd, max_concurrent)
    semantics_json = json.dumps(
        arm_semantics(clip_usd, max_concurrent), sort_keys=True, separators=(",", ":")
    )
    row = conn.execute(
        "SELECT policy_hash,policy_json,taxonomy_version,benchmark_version,regime_version,"
        "clip_usd,max_concurrent FROM hl_paper_arms WHERE arm_id=?", (aid,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO hl_paper_arms (arm_id,policy_hash,policy_json,taxonomy_version,"
            "benchmark_version,regime_version,clip_usd,max_concurrent,created_ts_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, phash, semantics_json, TAXONOMY_VERSION,
             BENCHMARK_VERSION, REGIME_VERSION, clip_usd, max_concurrent, now_ms))
    elif (row["policy_hash"] != phash or float(row["clip_usd"]) != float(clip_usd)
          or int(row["max_concurrent"]) != int(max_concurrent)
          or row["policy_json"] != semantics_json
          or row["taxonomy_version"] != TAXONOMY_VERSION
          or row["benchmark_version"] != BENCHMARK_VERSION
          or row["regime_version"] != REGIME_VERSION):
        raise RuntimeError("paper semantics changed under an existing arm id")
    return aid


# ---- market data ------------------------------------------------------------------------------

def live_universe():
    meta, _ctxs = _post_info({"type": "metaAndAssetCtxs"})
    return [str(a["name"]) for a in meta["universe"]
            if a.get("name") and not a.get("isDelisted") and str(a["name"]) not in MAJORS]


def completed_hourly_closes(rows, now_ms):
    """Return only provider candles whose declared close time is in the past."""
    return [(int(row["t"]), float(row["c"]))
            for row in sorted(rows, key=lambda row: int(row["t"]))
            if int(row.get("T", int(row["t"]) + HOUR_MS - 1)) < now_ms]


def hourly_closes(coin, hours=140, now_ms=None, start_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    body = _post_info({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1h",
        "startTime": int(start_ms) if start_ms is not None else now_ms - hours * HOUR_MS,
        "endTime": now_ms}})
    if not isinstance(body, list):
        return []
    return completed_hourly_closes(body, now_ms)


def causal_regime_snapshot(closes_by_coin, *, decision_ts_ms, universe):
    """Equal-weight trailing alt-basket regime using provider-exact symbols and past bars only."""
    anchor_ts = int(decision_ts_ms) - 72 * HOUR_MS
    values = []
    eligible = [str(coin) for coin in universe]
    for coin in eligible:
        rows = sorted(closes_by_coin.get(coin) or [], key=lambda item: int(item[0]))
        current = [row for row in rows if int(row[0]) == int(decision_ts_ms)]
        anchors = [row for row in rows if int(row[0]) <= anchor_ts]
        if not current or not anchors:
            continue
        current_px, anchor_px = float(current[-1][1]), float(anchors[-1][1])
        if current_px > 0 and anchor_px > 0:
            values.append((current_px - anchor_px) / anchor_px * 10_000.0)
    coverage = len(values) / len(eligible) if eligible else 0.0
    basket = sum(values) / len(values) if values else None
    if coverage < 0.80 or basket is None:
        label = "unknown"
    elif basket <= -500.0:
        label = "bearish"
    elif basket >= 500.0:
        label = "bullish"
    else:
        label = "neutral"
    return {
        "decision_ts_ms": int(decision_ts_ms), "eligible_n": len(eligible),
        "covered_n": len(values), "coverage": coverage,
        "basket_return_bps": basket, "label": label,
    }


def walk_book_notional(levels, notional_usd):
    """Sweep an exact dollar notional. Return None rather than extrapolating depth."""
    filled = qty = 0.0
    used = 0
    for lvl in levels:
        px, sz = float(lvl["px"]), float(lvl["sz"])
        if px <= 0 or sz <= 0:
            continue
        take = min(px * sz, notional_usd - filled)
        qty += take / px
        filled += take
        used += 1
        if filled >= notional_usd - 1e-9:
            break
    if filled < notional_usd - 1e-9 or qty <= 0:
        return None
    return {"px": filled / qty, "qty": qty, "levels": used}


def walk_book_quantity(levels, target_qty):
    """Sweep the exact base quantity opened, independent of its current dollar value."""
    filled_qty = cost = 0.0
    used = 0
    for lvl in levels:
        px, sz = float(lvl["px"]), float(lvl["sz"])
        if px <= 0 or sz <= 0:
            continue
        take = min(sz, target_qty - filled_qty)
        filled_qty += take
        cost += take * px
        used += 1
        if filled_qty >= target_qty - 1e-12:
            break
    if filled_qty < target_qty - 1e-12 or filled_qty <= 0:
        return None
    return {"px": cost / filled_qty, "qty": filled_qty, "levels": used}


def walk_book(levels, notional_usd):
    """Compatibility wrapper for the original unit-tested interface."""
    fill = walk_book_notional(levels, notional_usd)
    return (fill["px"], fill["levels"]) if fill else (None, 0)


def book_fill(coin, notional_usd, side, *, clip_qty=None, ref_qty=None):
    """side 'open_short' sweeps bids (you sell); 'close_short' sweeps asks (you buy back)."""
    book = fetch_l2_book(coin)
    levels = book["bids"] if side == "open_short" else book["asks"]
    if clip_qty is None:
        clip = walk_book_notional(levels, notional_usd)
        ref = walk_book_notional(levels, REFERENCE_NOTIONAL)
    else:
        clip = walk_book_quantity(levels, clip_qty)
        ref = walk_book_quantity(levels, ref_qty) if ref_qty else None
    return {
        "clip_px": clip["px"] if clip else None,
        "clip_qty": clip["qty"] if clip else None,
        "clip_levels": clip["levels"] if clip else None,
        "ref_px": ref["px"] if ref else None,
        "ref_qty": ref["qty"] if ref else None,
        "ref_levels": ref["levels"] if ref else None,
        "mid": book.get("mid"), "spread_bps": book.get("spread_bps"),
    }


def funding_self_pct(coin, funding_cache, now_ms):
    """Percentile of the trailing-24h mean funding within the coin's own trailing 30 days."""
    rates = funding_cache.get(coin) or {}
    if len(rates) < 240:
        return None, None
    recent = [rates[b] for b in range(now_ms - 24 * HOUR_MS, now_ms, HOUR_MS) if b in rates]
    if not recent:
        return None, None
    f24 = sum(recent) / len(recent) * 1e4
    hist = [rates[b] * 1e4 for b in range(now_ms - FUNDING_WINDOW_H * HOUR_MS, now_ms, HOUR_MS)
            if b in rates]
    if len(hist) < 240:
        return f24, None
    return f24, sum(1 for v in hist if v <= f24) / len(hist)


def seed_funding_cache(base_path="data/research/hl_full_history/funding"):
    """Seed 30d funding from the research cache; the running tape extends it."""
    cache, base = {}, Path(base_path)
    if base.exists():
        for path in base.glob("funding_*.json"):
            try:
                cache[path.stem.removeprefix("funding_")] = {
                    int(k): float(v) for k, v in json.loads(path.read_text()).items()}
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    return cache


def extend_funding_from_tape(cache, tape_path="data/hl_squeeze_tape.db", *, after_ts_ms=0):
    """Fold only new 60s tape samples into hourly funding and return the new cursor."""
    if not Path(tape_path).exists():
        return int(after_ts_ms)
    conn = sqlite3.connect(f"file:{tape_path}?mode=ro", uri=True)
    cursor = int(after_ts_ms)
    try:
        for coin, ts_ms, funding in conn.execute(
                "SELECT coin, ts_ms, funding FROM hl_squeeze_marks "
                "WHERE funding IS NOT NULL AND ts_ms>? ORDER BY ts_ms", (cursor,)):
            ts_ms = int(ts_ms)
            cache.setdefault(coin, {})[ts_ms // HOUR_MS * HOUR_MS] = float(funding)
            cursor = max(cursor, ts_ms)
    finally:
        conn.close()
    return cursor


def completed_provider_hour(now_ms):
    """Start timestamp of the newest fully closed provider hour."""
    return max(0, (int(now_ms) // HOUR_MS - 1) * HOUR_MS)


def nomination_episode(aid: str, coin: str, window_rows) -> dict:
    """One episode per exact causal trough. Price ties use the earliest timestamp."""
    trough_ts_ms, trough_px = min(
        ((int(ts_ms), float(px)) for ts_ms, px in window_rows),
        key=lambda item: (item[1], item[0]),
    )
    raw = f"{aid}|{coin}|{trough_ts_ms}|{trough_px}"
    return {
        "episode_id": hashlib.sha256(raw.encode()).hexdigest(),
        "trough_ts_ms": trough_ts_ms,
        "trough_px": trough_px,
    }


# ---- the loop ---------------------------------------------------------------------------------

def scan_and_open(
    conn, aid, coins, funding_cache, *, clip_usd, max_concurrent, now_ms, pace_s,
    should_stop=lambda: False, candle_cache=None, clock_ms=None,
):
    candle_cache = {} if candle_cache is None else candle_cache
    portfolio_open_n = conn.execute(
        "SELECT COUNT(*) FROM hl_paper_positions p JOIN hl_paper_observations o ON o.obs_id=p.obs_id "
        "WHERE p.arm_id=? AND p.status='open' AND o.portfolio_slot_eligible=1",
        (aid,)).fetchone()[0]
    opened = 0
    portfolio_opened = 0
    closes_by_coin = {}
    fetch_error_n = 0
    for coin in coins:
        if should_stop():
            return 0
        try:
            cached = candle_cache.get(coin) or []
            start_ms = int(cached[-1][0]) + HOUR_MS if cached else None
            fresh = hourly_closes(coin, now_ms=now_ms, start_ms=start_ms)
            merged = {int(ts): float(px) for ts, px in cached}
            merged.update({int(ts): float(px) for ts, px in fresh})
            closes_by_coin[coin] = sorted(merged.items())[-142:]
            candle_cache[coin] = closes_by_coin[coin]
        except Exception:
            closes_by_coin[coin] = []
            fetch_error_n += 1
        finally:
            time.sleep(pace_s)
    latest_counts = Counter(
        rows[-1][0] for rows in closes_by_coin.values() if rows
    )
    if not latest_counts:
        return 0
    decision_ts_ms = max(latest_counts, key=lambda ts: (latest_counts[ts], ts))
    latest_mismatch_n = sum(
        1 for rows in closes_by_coin.values() if rows and int(rows[-1][0]) != decision_ts_ms
    )
    regime = causal_regime_snapshot(
        closes_by_coin, decision_ts_ms=decision_ts_ms, universe=coins
    )
    conn.execute(
        "INSERT OR IGNORE INTO hl_paper_regimes (arm_id,decision_ts_ms,label,basket_return_bps,"
        "coverage,eligible_n,covered_n,created_ts_ms) VALUES (?,?,?,?,?,?,?,?)",
        (aid, decision_ts_ms, regime["label"], regime["basket_return_bps"], regime["coverage"],
         regime["eligible_n"], regime["covered_n"], now_ms),
    )
    conn.execute(
        "UPDATE hl_paper_regimes SET fetch_error_n=?,latest_mismatch_n=? "
        "WHERE arm_id=? AND decision_ts_ms=?",
        (fetch_error_n, latest_mismatch_n, aid, decision_ts_ms),
    )
    for coin in coins:
        held = conn.execute(
            "SELECT 1 FROM hl_paper_positions WHERE arm_id=? AND coin=? AND status='open'",
            (aid, coin)).fetchone()
        if held:
            continue
        closes = closes_by_coin.get(coin) or []
        if len(closes) < POLICY["runup_lookback_h"] + 2:
            continue
        n = POLICY["runup_lookback_h"]
        cur_t, cur_c = closes[-1]
        if cur_t != decision_ts_ms:
            continue
        prev_t, prev_c = closes[-2]
        window_now_rows = closes[-(n + 1):-1]
        window_prev_rows = closes[-(n + 2):-2]
        window_now = [c for _t, c in window_now_rows]
        window_prev = [c for _t, c in window_prev_rows]
        if not window_now or not window_prev or min(window_now) <= 0 or min(window_prev) <= 0:
            continue
        runup = cur_c / min(window_now) - 1.0
        runup_prev = prev_c / min(window_prev) - 1.0
        if not (runup >= POLICY["runup_pct"] > runup_prev):
            continue                                    # crossing only, never a level
        episode = nomination_episode(aid, coin, window_now_rows)
        episode_primary = int(conn.execute(
            "SELECT 1 FROM hl_paper_observations WHERE arm_id=? AND coin=? AND episode_id=?",
            (aid, coin, episode["episode_id"]),
        ).fetchone() is None)
        obs_id = hashlib.sha256(f"{aid}|{coin}|{cur_t}".encode()).hexdigest()[:32]
        if conn.execute("SELECT 1 FROM hl_paper_observations WHERE obs_id=?", (obs_id,)).fetchone():
            continue
        f24, pct = funding_self_pct(coin, funding_cache, cur_t)
        if pct is None:
            gated, reason = 0, "funding_percentile_unavailable"
        elif pct > POLICY["funding_self_pct_max"]:
            gated, reason = 0, "funding_top5pct_skip"
        else:
            gated, reason = 1, "accepted"
        slot_eligible = int(
            episode_primary and portfolio_open_n + portfolio_opened < max_concurrent
        )
        entry_status = "pending" if gated and episode_primary else "stood_down"
        entry_reason = None if episode_primary else "repeat_same_episode"
        conn.execute(
            "INSERT INTO hl_paper_observations (obs_id,arm_id,coin,decision_ts_ms,decision_close,"
            "runup_72h,funding_24h_bps,funding_self_pct,gated_in,gate_reason,created_ts_ms,"
            "portfolio_slot_eligible,entry_status,entry_reason,episode_id,episode_primary,"
            "trough_ts_ms,trough_px,regime_label,regime_basket_bps,regime_coverage) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obs_id, aid, coin, cur_t, cur_c, runup, f24, pct, gated, reason, now_ms,
             slot_eligible, entry_status, entry_reason, episode["episode_id"], episode_primary,
             episode["trough_ts_ms"], episode["trough_px"],
             regime["label"], regime["basket_return_bps"],
             regime["coverage"]))
        logger.info("NOMINATION %s runup=%.1f%% funding_pct=%s -> %s",
                    coin, runup * 100, "n/a" if pct is None else f"{pct:.3f}", reason)
        if not gated or not episode_primary:
            continue
        entry_hour_start = cur_t + HOUR_MS
        if not entry_hour_start <= now_ms < entry_hour_start + HOUR_MS:
            conn.execute(
                "UPDATE hl_paper_observations SET entry_status='rejected',entry_reason=? "
                "WHERE obs_id=?", ("entry_window_missed", obs_id))
            continue
        try:
            fill = book_fill(coin, clip_usd, "open_short")
        except Exception as exc:
            logger.warning("book unavailable for %s: %s", coin, exc)
            conn.execute(
                "UPDATE hl_paper_observations SET entry_status='rejected',entry_reason=? "
                "WHERE obs_id=?", ("entry_book_unavailable", obs_id))
            continue
        fill_ts_ms = int(clock_ms()) if clock_ms is not None else int(now_ms)
        if not entry_hour_start <= fill_ts_ms < entry_hour_start + HOUR_MS:
            conn.execute(
                "UPDATE hl_paper_observations SET entry_status='rejected',entry_reason=? "
                "WHERE obs_id=?", ("entry_window_expired_during_scan", obs_id))
            continue
        if not fill["clip_px"]:
            logger.warning("insufficient depth to short %s at $%s", coin, clip_usd)
            conn.execute(
                "UPDATE hl_paper_observations SET entry_status='rejected',entry_reason=? "
                "WHERE obs_id=?", ("entry_depth_insufficient", obs_id))
            continue
        entry = fill["clip_px"]
        conn.execute(
            "INSERT INTO hl_paper_positions (position_id,arm_id,obs_id,coin,status,clip_usd,"
            "entry_ts_ms,entry_px_clip,entry_qty_clip,entry_px_ref,entry_qty_ref,entry_mid,"
            "entry_spread_bps,entry_levels_clip,entry_levels_ref,stop_px,target_px,deadline_ts_ms) "
            "VALUES (?,?,?,?, 'open', ?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obs_id, aid, obs_id, coin, clip_usd, fill_ts_ms, entry, fill["clip_qty"],
             fill["ref_px"], fill["ref_qty"], fill["mid"], fill["spread_bps"],
             fill.get("clip_levels"), fill.get("ref_levels"), entry * POLICY["stop_loss_mult"],
             entry * POLICY["take_profit_mult"], fill_ts_ms + POLICY["cap_hours"] * HOUR_MS))
        conn.execute(
            "UPDATE hl_paper_observations SET entry_status='opened',entry_reason=NULL WHERE obs_id=?",
            (obs_id,))
        opened += 1
        portfolio_opened += slot_eligible
        logger.info("OPENED SHORT %s $%.0f @ %.8g (ref5k %.8g) stop %.8g target %.8g",
                    coin, clip_usd, entry, fill["ref_px"] or -1,
                    entry * POLICY["stop_loss_mult"], entry * POLICY["take_profit_mult"])
    return opened


def manage_open(conn, aid, *, now_ms, pace_s):
    closed = 0
    for pos in conn.execute(
            "SELECT * FROM hl_paper_positions WHERE arm_id=? AND status='open'", (aid,)).fetchall():
        try:
            fill = book_fill(pos["coin"], pos["clip_usd"], "close_short",
                             clip_qty=pos["entry_qty_clip"], ref_qty=pos["entry_qty_ref"])
        except Exception:
            continue
        time.sleep(pace_s)
        px = fill["clip_px"]
        if px is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO hl_paper_marks (position_id,ts_ms,mid,book_px_clip,spread_bps) "
            "VALUES (?,?,?,?,?)",
            (pos["position_id"], now_ms, fill["mid"], px, fill["spread_bps"]))
        reason = None
        if px >= pos["stop_px"]:
            reason = "stop"
        elif px <= pos["target_px"]:
            reason = "target"
        elif now_ms >= pos["deadline_ts_ms"]:
            reason = "cap"
        if reason is None:
            continue
        entry = pos["entry_px_clip"]
        gross_clip = (entry - px) / entry * 1e4
        gross_ref = ((pos["entry_px_ref"] - fill["ref_px"]) / pos["entry_px_ref"] * 1e4
                     if pos["entry_px_ref"] and fill["ref_px"] else None)
        try:
            rows = fetch_funding_history(pos["coin"], pos["entry_ts_ms"], now_ms)
            funding = FUND.funding_pnl(
                rows, side="short", entry_ts_ms=pos["entry_ts_ms"], exit_ts_ms=now_ms,
            )
        except Exception:
            funding = FUND.funding_pnl(
                None, side="short", entry_ts_ms=pos["entry_ts_ms"], exit_ts_ms=now_ms,
                fetch_failed=True,
            )
        fund_bps = funding.value_bps
        funding_status = funding.status
        # what the backtest would have booked: exit AT the stop/target, no book sweep
        modelled_px = (pos["stop_px"] if reason == "stop"
                       else pos["target_px"] if reason == "target" else fill["mid"])
        modelled = ((entry - modelled_px) / entry * 1e4 if modelled_px is not None else None)
        net_clip = (gross_clip + fund_bps - TAKER_FEE_BPS_ROUND_TRIP
                    if fund_bps is not None else None)
        net_ref = (gross_ref + fund_bps - TAKER_FEE_BPS_ROUND_TRIP
                   if gross_ref is not None and fund_bps is not None else None)
        fill_delta = gross_clip - modelled if modelled is not None else None
        conn.execute(
            "UPDATE hl_paper_positions SET status='closed', exit_ts_ms=?, exit_px_clip=?, "
            "exit_px_ref=?, exit_reason=?,exit_mid=?,exit_spread_bps=?,exit_levels_clip=?,"
            "exit_levels_ref=?,funding_bps=?,funding_pnl_bps=?,funding_status=?,"
            "funding_provenance_status=?,"
            "gross_bps_clip=?,gross_bps_ref=?, "
            "net_bps_clip=?, net_bps_ref=?, modelled_gross_bps=?, fill_delta_bps=? "
            "WHERE position_id=?",
            (now_ms, px, fill["ref_px"], reason, fill["mid"], fill["spread_bps"],
             fill.get("clip_levels"), fill.get("ref_levels"), fund_bps, fund_bps, funding_status,
             funding_status,
             gross_clip, gross_ref, net_clip, net_ref, modelled, fill_delta, pos["position_id"]))
        closed += 1
        logger.info("CLOSED %s %s net=%s bps (modelled %s, fill delta %s) funding=%s",
                    pos["coin"], reason,
                    "n/a" if net_clip is None else f"{net_clip:+.1f}",
                    "n/a" if modelled is None else f"{modelled:+.1f}",
                    "n/a" if fill_delta is None else f"{fill_delta:+.1f}",
                    "n/a" if fund_bps is None else f"{fund_bps:+.1f}")
    return closed


def heartbeat(path, payload):
    tmp = f"{path}.tmp"
    Path(tmp).write_text(json.dumps(payload, sort_keys=True))
    os.replace(tmp, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="HL alt run-up short paper ledger (no funds)")
    parser.add_argument("--db-path", default="data/hl_altshort_paper.db")
    parser.add_argument("--heartbeat", default="data/hl_altshort_paper.heartbeat")
    parser.add_argument("--clip-usd", type=float, default=40.0)
    parser.add_argument("--max-concurrent", type=int, default=5)
    parser.add_argument("--manage-seconds", type=float, default=60.0)
    parser.add_argument("--scan-seconds", type=float, default=900.0)
    parser.add_argument("--pace-seconds", type=float, default=0.25)
    parser.add_argument("--funding-cache-dir", default="data/research/hl_full_history/funding")
    parser.add_argument("--funding-tape", default="data/hl_squeeze_tape.db")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.clip_usd <= 0 or args.max_concurrent < 1:
        parser.error("clip must be > 0 and max-concurrent >= 1")

    # Single-instance lock held for the PROCESS LIFETIME, not just the watchdog invocation.
    # The watchdog matches a running process by "$CODE_ROOT/scripts/run_hl_altshort_paper.py $ARGS", and
    # CODE_ROOT resolves through .runtime/hyperliquid/current — so repointing that symlink makes
    # the running process stop matching and the next cron tick starts a SECOND one against the
    # same DB. Two release repoints on 2026-08-03 left three copies of this worker writing one
    # ledger (visible as 2-4 ticks/minute on a 1/minute cadence). Only a lock the runner itself
    # holds can prevent that; a watchdog-side flock serialises watchdogs, not workers.
    _run_lock = acquire_run_lock(os.getenv(
        "HL_ALTSHORT_PAPER_RUN_LOCK", str(Path(args.db_path).resolve().parent / "hl_altshort_paper.run.lock")))
    if _run_lock is None:
        print("another run_hl_altshort_paper holds the run lock — refusing to start", file=sys.stderr)
        return 0


    stop = {"stop": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_a: stop.update(stop=True))

    conn = connect(args.db_path)
    now_ms = int(time.time() * 1000)
    aid = ensure_arm(conn, args.clip_usd, args.max_concurrent, now_ms)
    logger.info("HL PAPER — NO FUNDS arm=%s clip=$%.0f max_concurrent=%d policy=%s",
                aid, args.clip_usd, args.max_concurrent,
                policy_hash(args.clip_usd, args.max_concurrent)[:12])

    funding_cache = seed_funding_cache(args.funding_cache_dir)
    funding_cursor = extend_funding_from_tape(funding_cache, args.funding_tape)
    universe = live_universe()
    logger.info("universe=%d live alts; funding history for %d coins",
                len(universe), len(funding_cache))
    initial_counts = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM hl_paper_positions WHERE arm_id=? AND status='open'),"
        "(SELECT COUNT(*) FROM hl_paper_positions WHERE arm_id=? AND status='closed'),"
        "(SELECT COUNT(*) FROM hl_paper_observations WHERE arm_id=?)",
        (aid, aid, aid),
    ).fetchone()
    heartbeat(args.heartbeat, {
        "arm_id": aid,
        "policy_hash": policy_hash(args.clip_usd, args.max_concurrent),
        "ts_ms": int(time.time() * 1000),
        "phase": "scanning",
        "clip_usd": args.clip_usd,
        "max_concurrent": args.max_concurrent,
        "open": initial_counts[0],
        "closed": initial_counts[1],
        "observations": initial_counts[2],
        "universe": len(universe),
    })

    next_scan = 0.0
    last_scan_hour = None
    candle_cache = {}
    scan_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hl-altshort-scan")
    scan_future = None

    def run_scan(scan_now_ms):
        scan_conn = connect(args.db_path)
        try:
            return scan_and_open(
                scan_conn, aid, list(universe), funding_cache,
                clip_usd=args.clip_usd, max_concurrent=args.max_concurrent,
                now_ms=scan_now_ms, pace_s=args.pace_seconds,
                should_stop=lambda: stop["stop"], candle_cache=candle_cache,
                clock_ms=lambda: int(time.time() * 1000),
            )
        finally:
            scan_conn.close()

    while True:
        now_ms = int(time.time() * 1000)
        closed = manage_open(conn, aid, now_ms=now_ms, pace_s=args.pace_seconds)
        opened = 0
        if scan_future is not None and scan_future.done():
            try:
                opened = int(scan_future.result())
            except Exception as exc:
                logger.exception("background scan failed: %s", exc)
            scan_future = None
        scan_hour = completed_provider_hour(now_ms)
        if (scan_future is None and time.monotonic() >= next_scan
                and scan_hour != last_scan_hour):
            next_scan = time.monotonic() + args.scan_seconds
            funding_cursor = extend_funding_from_tape(
                funding_cache, args.funding_tape, after_ts_ms=funding_cursor
            )
            try:
                universe = live_universe()
            except Exception:
                pass
            if args.once:
                opened = scan_and_open(
                    conn, aid, universe, funding_cache,
                    clip_usd=args.clip_usd, max_concurrent=args.max_concurrent,
                    now_ms=now_ms, pace_s=args.pace_seconds,
                    should_stop=lambda: stop["stop"], candle_cache=candle_cache,
                    clock_ms=lambda: int(time.time() * 1000),
                )
            else:
                scan_future = scan_pool.submit(run_scan, now_ms)
            last_scan_hour = scan_hour
        counts = conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM hl_paper_positions WHERE arm_id=? AND status='open') AS open_n,"
            "(SELECT COUNT(*) FROM hl_paper_positions WHERE arm_id=? AND status='closed') AS closed_n,"
            "(SELECT COUNT(*) FROM hl_paper_observations WHERE arm_id=?) AS observations_n,"
            "(SELECT COUNT(*) FROM hl_paper_observations WHERE arm_id=? AND gated_in=1) AS accepted_n,"
            "(SELECT COUNT(*) FROM hl_paper_observations WHERE arm_id=? AND entry_status='rejected') "
            "AS rejected_n,"
            "(SELECT COUNT(*) FROM hl_paper_positions WHERE arm_id=? AND status='closed' "
            "AND COALESCE(funding_provenance_status,funding_status) "
            "NOT IN ('complete','not_applicable')) "
            "AS funding_missing_n",
            (aid, aid, aid, aid, aid, aid),
        ).fetchone()
        heartbeat(args.heartbeat, {
            "arm_id": aid,
            "policy_hash": policy_hash(args.clip_usd, args.max_concurrent), "ts_ms": now_ms,
            "phase": "idle",
            "clip_usd": args.clip_usd, "max_concurrent": args.max_concurrent,
            "open": counts["open_n"], "closed": counts["closed_n"],
            "observations": counts["observations_n"], "accepted": counts["accepted_n"],
            "rejected": counts["rejected_n"], "funding_missing": counts["funding_missing_n"],
            "universe": len(universe), "opened_this_cycle": opened, "closed_this_cycle": closed,
        })
        if args.once or stop["stop"]:
            scan_pool.shutdown(wait=False, cancel_futures=True)
            return 0
        slept = 0.0
        while slept < args.manage_seconds and not stop["stop"]:
            time.sleep(min(0.5, args.manage_seconds - slept))
            slept += 0.5


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())

"""Desk INTAKE phase 1 (operator 2026-10-27, Alpha): COLLECT ONLY. One adapter per feed writes rows
(feed, filter, network_id, mint, venue, provider rank, provider fields raw, observed_at) to
data/desk/intake/<feed>/<day>.db, plus the first sighting per (feed, network_id, mint) (Gamma prereg 74204a4ed
section 1). No seat, no JUDGE, no entry, and no provider value is ever a fill, price or
mark. Nothing reads these files yet.

Every pass writes ONE polls row per (feed, filter) with its outcome: an error is a named reason, never an empty
success. Raw fields are stored verbatim (absent stays absent; a provider's 0 stays 0). observed_at is OUR receive
time on the corrected UTC clock (host receive time minus data/clock_offset.json), never a provider timestamp; NULL
when no offset is known (host_received_at and clock_offset_s are kept, so it is never silently host time). A first
sighting is written once and never moved earlier: no backfill, no backdating.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS polls (id INTEGER PRIMARY KEY, feed TEXT NOT NULL, filter TEXT NOT NULL,
    received_at REAL NOT NULL, status TEXT NOT NULL, items INTEGER, malformed INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS intake_rows (id INTEGER PRIMARY KEY, poll_id INTEGER NOT NULL, feed TEXT NOT NULL,
    filter TEXT NOT NULL, network_id TEXT, chain_raw TEXT NOT NULL, mint TEXT NOT NULL, venue TEXT,
    provider_rank INTEGER, rank_kind TEXT, raw_json TEXT NOT NULL, observed_at REAL, host_received_at REAL NOT NULL,
    clock_offset_s REAL, source_ref TEXT NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS intake_rows_mint ON intake_rows(network_id, mint, observed_at);
CREATE TABLE IF NOT EXISTS cursors (source TEXT PRIMARY KEY, last_rowid INTEGER NOT NULL);
"""
# ONE file per feed for the whole history (Delta a00bff003 P2-2): a coin first seen yesterday has no second t0 today.
FIRST_DDL = """
CREATE TABLE IF NOT EXISTS fs.first_sightings (feed TEXT NOT NULL, network_id TEXT NOT NULL, mint TEXT NOT NULL,
    day TEXT NOT NULL, row_id INTEGER NOT NULL, filter TEXT NOT NULL, observed_at REAL NOT NULL,
    chain_raw TEXT, venue TEXT, provider_rank INTEGER, rank_kind TEXT, raw_json TEXT, host_received_at REAL,
    clock_offset_s REAL, PRIMARY KEY (feed, network_id, mint));
CREATE TABLE IF NOT EXISTS fs.polls_archive (day TEXT NOT NULL, id INTEGER NOT NULL, feed TEXT NOT NULL,
    filter TEXT NOT NULL, received_at REAL NOT NULL, status TEXT NOT NULL, items INTEGER, malformed INTEGER, error TEXT,
    PRIMARY KEY (day, id));
"""
# first_sightings is SELF-CONTAINED (Gamma): the first row's fields are copied in, so pruning a day file never breaks
# the read; polls_archive keeps every day's poll rows (gap/error windows) before its file is pruned.

# provider chain id -> our network_id (sniper.execution.solana_quote.FOMO_TO_DEX inverted); unknown = NULL, kept
from sniper.execution.solana_quote import DEX_CHAIN_TO_FOMO  # noqa: E402


def clock_offset(path):
    """The learned host-minus-UTC offset in seconds, or None when unknown (missing / unreadable file). Unlike
    sniper.util.clock.offset_s, unknown is never 0."""
    try:
        v = json.loads(Path(path).read_text())["offset_s"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return float(v) if type(v) in (int, float) and math.isfinite(v) else None   # NaN / bool / text = unknown (Delta)


def clock_sample(path, *, now):
    """(offset_s, file at) from ONE read of clock_offset.json, or (None, None) when either field is unknown
    (missing, non-number, bool, non-finite) or the sample is older than the D sheet max age (memecoin_desk_contract.CLOCK_MAX_AGE_S). One read so a refresh
    between two reads can never pair an old offset with a new 'at' (Zeta HOLD c3593cd13)."""
    try:
        doc = json.loads(Path(path).read_text())
        offset, at = doc["offset_s"], doc["at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if (type(offset) not in (int, float) or type(at) not in (int, float) or not math.isfinite(offset)
            or not math.isfinite(at) or not 0 <= now - at <= _clock_max_age_s()):
        return None, None
    return float(offset), float(at)


def _clock_max_age_s():
    from sniper.services.memecoin_desk_contract import CLOCK_MAX_AGE_S   # hash-bound in D_SHEET_DEFINITION
    return CLOCK_MAX_AGE_S


# DexScreener endpoint -> filter label (feed "dexscreener"). Boosts, profiles, takeovers and ads are PAID or self-submitted promotion,
# never "trending"; metas carry no token and are not candidates.
DEXSCREENER_FEEDS = {"/token-boosts/top/v1": "boost_top", "/token-boosts/latest/v1": "boost_latest",
                     "/token-profiles/latest/v1": "profile", "/community-takeovers/latest/v1": "takeover",
                     "/ads/latest/v1": "ad"}


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def open_day(out_dir, feed, at):
    path = Path(out_dir) / feed
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path / ("%s.db" % utc_day(at)), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    conn.execute("ATTACH DATABASE ? AS fs", (str(path / "first_sightings.db"),))
    conn.execute("PRAGMA fs.journal_mode=WAL")
    conn.executescript(FIRST_DDL)
    _migrate_first(conn)
    return conn


FIRST_ADDED = (("chain_raw", "TEXT"), ("venue", "TEXT"), ("provider_rank", "INTEGER"), ("rank_kind", "TEXT"),
               ("raw_json", "TEXT"), ("host_received_at", "REAL"), ("clock_offset_s", "REAL"))


def _migrate_first(conn):
    """Additive migration of a pre-601c619d1 fs.first_sightings (Delta): add the self-contained columns, then
    backfill them from this open day file's rows the old t0s point to. Rows of other days are backfilled when that
    day is opened, and prune() never deletes a day whose first sightings are still unfilled."""
    have = {r[1] for r in conn.execute("PRAGMA fs.table_info(first_sightings)")}
    for name, kind in FIRST_ADDED:
        if name not in have:
            conn.execute("ALTER TABLE fs.first_sightings ADD COLUMN %s %s" % (name, kind))
    day = _day_of(conn)
    conn.execute("UPDATE fs.first_sightings SET (chain_raw, venue, provider_rank, rank_kind, raw_json, host_received_at, "
                 "clock_offset_s) = (SELECT chain_raw, venue, provider_rank, rank_kind, raw_json, host_received_at, "
                 "clock_offset_s FROM main.intake_rows r WHERE r.id=fs.first_sightings.row_id) "
                 "WHERE day=? AND raw_json IS NULL", (day,))
    conn.commit()


def _day_of(conn):
    return Path([d[2] for d in conn.execute("PRAGMA database_list").fetchall() if d[1] == "main"][0]).stem


def _first(conn, feed, network_id, mint, row_id, filt, observed):
    """Record t0 once per (feed, network_id, mint) across all days; never moved earlier, never overwritten."""
    day = _day_of(conn)
    conn.execute("INSERT OR IGNORE INTO fs.first_sightings (feed, network_id, mint, day, row_id, filter, observed_at, "
                 "chain_raw, venue, provider_rank, rank_kind, raw_json, host_received_at, clock_offset_s) "
                 "SELECT ?,?,?,?,id,?,?,chain_raw,venue,provider_rank,rank_kind,raw_json,host_received_at,clock_offset_s "
                 "FROM main.intake_rows WHERE id=?", (feed, network_id, mint, day, filt, observed, row_id))


def norm_mint(chain, mint):
    """EVM 0x hex is lowercased; anything else (Solana base58, unknown chains) is case-sensitive and kept as-is.
    None when not a non-empty string."""
    if not isinstance(chain, str) or not chain or not isinstance(mint, str) or not mint.strip():
        return None
    mint = mint.strip()
    return mint.lower() if mint[:2].lower() == "0x" else mint


def dexscreener_pass(source_db, out_conn, *, now, offset_s):
    """Mirror new DexScreener producer polls (scripts/memecoin_desk_dexscreener.py) from one source day file: one
    intake polls row per source poll, its outcome copied (an error or a 429 stays an error, an empty list stays a real
    empty: Delta P2-1), and its token items as intake rows. Zero HTTP. Cursor = source polls id, so a rerun inserts
    nothing twice; the producer writes a poll and its items in one transaction. offset_s = clock_offset(...) read once
    by the caller (None = unknown). Rank = the item's position in its poll (rank_kind 'list_position')."""
    source = Path(source_db)
    stats = {"items": 0, "malformed": 0, "source_errors": 0}
    try:
        src = sqlite3.connect("file:%s?mode=ro" % source, uri=True, timeout=5)
        try:
            row = out_conn.execute("SELECT last_rowid FROM cursors WHERE source=?", (source.name,)).fetchone()
            last = row[0] if row else 0
            polls = src.execute("SELECT id, endpoint, received_at, status, items, error FROM polls WHERE id>? ORDER BY id",
                                (last,)).fetchall()
            items = {}
            for poll_id, chain, token, payload in src.execute(
                    "SELECT poll_id, chain_id, token_address, payload_json FROM items WHERE poll_id>? ORDER BY rowid", (last,)):
                items.setdefault(poll_id, []).append((chain, token, payload))
        finally:
            src.close()
    except sqlite3.Error as exc:
        error = "source_missing" if not source.exists() else "source_unreadable:%s" % type(exc).__name__
        out_conn.execute("INSERT INTO polls (feed, filter, received_at, status, error) VALUES (?,?,?,?,?)",
                         ("dexscreener", "*", now, "error", error))
        out_conn.commit()
        return {**stats, "error": error}
    for poll_id, endpoint, received_at, status, n, error in polls:
        last = max(last, poll_id)
        feed = DEXSCREENER_FEEDS.get(endpoint)
        if feed is None:
            continue                                   # metas: no token
        if error:
            out_conn.execute("INSERT INTO polls (feed, filter, received_at, status, error) VALUES (?,?,?,?,?)",
                             ("dexscreener", feed, received_at, "error", "source:%s" % error))
            stats["source_errors"] += 1
            continue
        cur = out_conn.execute("INSERT INTO polls (feed, filter, received_at, status) VALUES (?,?,?,?)",
                               ("dexscreener", feed, received_at, "ok"))
        observed = None if offset_s is None else received_at - offset_s
        good = bad = 0
        for i, (chain, token, payload) in enumerate(items.get(poll_id, [])):
            mint = norm_mint(chain, token)
            try:
                metrics = json.loads(payload)
            except ValueError:
                metrics = None
            if mint is None or not isinstance(metrics, dict):
                bad += 1
                continue
            network_id = DEX_CHAIN_TO_FOMO.get(chain)
            ins = out_conn.execute(
                "INSERT OR IGNORE INTO intake_rows (poll_id, feed, filter, network_id, chain_raw, mint, venue, provider_rank, "
                "rank_kind, raw_json, observed_at, host_received_at, clock_offset_s, source_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cur.lastrowid, "dexscreener", feed, network_id, chain, mint, None, i + 1, "list_position",
                 json.dumps(metrics, sort_keys=True), observed, received_at, offset_s, "%s:%d:%d" % (source.name, poll_id, i)))
            if ins.rowcount and network_id is not None and observed is not None:
                _first(out_conn, "dexscreener", network_id, mint, ins.lastrowid, feed, observed)
            good += 1
        out_conn.execute("UPDATE polls SET items=?, malformed=? WHERE id=?", (good, bad, cur.lastrowid))
        stats["items"] += good
        stats["malformed"] += bad
    out_conn.execute("INSERT INTO cursors (source, last_rowid) VALUES (?,?) ON CONFLICT(source) DO UPDATE SET "
                     "last_rowid=excluded.last_rowid", (source.name, last))
    out_conn.commit()
    return stats


def record_poll(conn, feed, filt, at, rows, *, offset_s, error=None):
    """The shared writer every adapter uses (Pump.fun here; Codex's Fomo adapter): one polls row for this (feed,
    filter) pass, then its rows in list order. `rows` = [None (malformed, counted) | {network_id (ours or None),
    chain_raw, mint (already norm_mint), venue or None, raw (dict, stored verbatim)}]; rank = 1-based list position.
    `error` = a named failure: the polls row says so and no row is written (never empty-on-error). `at` = host receive
    time; observed_at = at - offset_s, NULL when the offset is unknown; t0 goes to the feed-wide first_sightings."""
    if error:
        conn.execute("INSERT INTO polls (feed, filter, received_at, status, error) VALUES (?,?,?,?,?)",
                     (feed, filt, at, "error", error))
        conn.commit()
        return {"items": 0, "malformed": 0}
    cur = conn.execute("INSERT INTO polls (feed, filter, received_at, status) VALUES (?,?,?,?)", (feed, filt, at, "ok"))
    observed = None if offset_s is None else at - offset_s
    good = bad = 0
    for i, r in enumerate(rows):
        if r is None:
            bad += 1
            continue
        ins = conn.execute(
            "INSERT OR IGNORE INTO intake_rows (poll_id, feed, filter, network_id, chain_raw, mint, venue, provider_rank, "
            "rank_kind, raw_json, observed_at, host_received_at, clock_offset_s, source_ref) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cur.lastrowid, feed, filt, r["network_id"], r["chain_raw"], r["mint"], r["venue"], i + 1, "list_position",
             json.dumps(r["raw"], sort_keys=True), observed, at, offset_s, "%s:%d:%d" % (feed, cur.lastrowid, i)))
        if ins.rowcount and observed is not None and r["network_id"] is not None:
            _first(conn, feed, r["network_id"], r["mint"], ins.lastrowid, filt, observed)
        good += 1
    conn.execute("UPDATE polls SET items=?, malformed=? WHERE id=?", (good, bad, cur.lastrowid))
    conn.commit()
    return {"items": good, "malformed": bad}


# ------------------------------------------------------------------ Pump.fun (public frontend API, plain HTTP)
PUMPFUN_BASE = "https://frontend-api-v3.pump.fun"
# filter label -> the list the site itself serves (its own default boards). No 'trending' list was found on the
# public API (2026-10-27 probe: king-of-the-hill / for-you 404), so none is claimed.
PUMPFUN_LISTS = {
    "live": "/coins/currently-live?limit=50&offset=0&includeNsfw=false",
    "active": "/coins?offset=0&limit=50&sort=last_trade_timestamp&order=DESC&includeNsfw=false",
    "top_mcap": "/coins?offset=0&limit=50&sort=market_cap&order=DESC&includeNsfw=false&complete=false",
    "new": "/coins?offset=0&limit=50&sort=created_timestamp&order=DESC&includeNsfw=false",
}
SOLANA_MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
CHALLENGE_MARKERS = ("Just a moment", "cf-chl", "challenge-platform", "turnstile", "captcha")


def http_get(url, timeout=10.0):
    """(status, parsed JSON or None, error slug or None). Never raises. A challenge page is 'refused:challenge'."""
    import http.client
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sniper-desk/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:                                            # a stalled/broken error body must not hide the status (Delta)
            body = exc.read(4000).decode("utf-8", "replace")
        except Exception:
            body = ""
        finally:
            exc.close()
        if any(m in body for m in CHALLENGE_MARKERS):
            return exc.code, None, "refused:challenge"
        return exc.code, None, ("refused:http_%d" % exc.code) if exc.code in (401, 403, 429) else "http_%d" % exc.code
    except (TimeoutError, OSError, ValueError, http.client.HTTPException):
        return None, None, "transport"                  # incl. a truncated body (IncompleteRead is an HTTPException, Delta)
    try:
        return status, json.loads(body), None
    except ValueError:
        text = body[:4000].decode("utf-8", "replace")
        return status, None, "refused:challenge" if any(m in text for m in CHALLENGE_MARKERS) else "malformed_json"


def pumpfun_pass(out_conn, *, get=http_get, clock=None, offset_s):
    """One GET per Pump.fun list. Returns (stats, refused): refused=True on any refusal (403/429/challenge), and the
    caller backs off; the rest of the pass is skipped, never retried through a challenge."""
    import time
    clock = clock or time.time
    stats = {"items": 0, "malformed": 0, "errors": 0}
    for name, path in PUMPFUN_LISTS.items():
        try:                                            # ONE guard at the fetch+read+parse boundary (Alpha): any exception
            status, payload, err = get(PUMPFUN_BASE + path)   # is a typed poll row, never an escaped crash
        except Exception as exc:
            logging.getLogger(__name__).exception("desk intake fetch failed feed=pumpfun filter=%s", name)
            status, payload, err = None, None, "exception:%s" % type(exc).__name__
        at = clock()
        if err is None and (status != 200 or not isinstance(payload, list)):
            err = "http_%s" % status if status != 200 else "unexpected_shape"
        if err:
            record_poll(out_conn, "pumpfun", name, at, [], offset_s=offset_s, error=err)
            stats["errors"] += 1
            if err.startswith("refused"):
                return stats, True
            continue
        rows = []
        for coin in payload:
            chain = coin.get("chain_id") if isinstance(coin, dict) else None
            mint = norm_mint(chain, coin.get("mint")) if isinstance(chain, str) else None
            if mint is None:
                rows.append(None)
                continue
            complete = coin.get("complete")
            rows.append({"network_id": "solana" if chain == SOLANA_MAINNET_CAIP2 else None,   # multichain: unmapped = NULL
                         "chain_raw": chain, "mint": mint,
                         "venue": None if complete is None else ("pumpswap" if complete else "pump_curve"), "raw": coin})
        got = record_poll(out_conn, "pumpfun", name, at, rows, offset_s=offset_s)
        stats["items"] += got["items"]
        stats["malformed"] += got["malformed"]
    return stats, False


def prune(feed_dir, *, now, keep_days):
    """Delete this feed's day files older than keep_days UTC days, after copying each day's polls into
    fs.polls_archive (Gamma: gap/error windows survive). first_sightings is self-contained and never pruned. A day
    whose polls cannot be archived is kept. Returns the pruned day names. Gated by the caller's knob (Gamma posts
    the feed's results doc first)."""
    feed_dir = Path(feed_dir)
    oldest = utc_day(now - keep_days * 86400)
    pruned = []
    for f in sorted(feed_dir.glob("????-??-??.db")):
        day = f.stem
        if day >= oldest:
            continue
        try:
            conn = sqlite3.connect(f, timeout=30)
            try:
                conn.execute("ATTACH DATABASE ? AS fs", (str(feed_dir / "first_sightings.db"),))
                conn.executescript(FIRST_DDL)
                _migrate_first(conn)
                if conn.execute("SELECT 1 FROM fs.first_sightings WHERE day=? AND raw_json IS NULL LIMIT 1",
                                (day,)).fetchone():
                    continue                            # a t0 of this day could not be made self-contained: keep the day
                conn.execute("INSERT OR IGNORE INTO fs.polls_archive (day, id, feed, filter, received_at, status, items, "
                             "malformed, error) SELECT ?, id, feed, filter, received_at, status, items, malformed, error "
                             "FROM main.polls", (day,))
                conn.commit()
                n_src = conn.execute("SELECT COUNT(*) FROM main.polls").fetchone()[0]
                n_arc = conn.execute("SELECT COUNT(*) FROM fs.polls_archive WHERE day=?", (day,)).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            logging.getLogger(__name__).exception("desk intake prune: archive failed, day kept dir=%s day=%s", feed_dir, day)
            continue
        if n_arc < n_src:
            continue
        for p in feed_dir.glob(day + ".db*"):
            p.unlink(missing_ok=True)
        pruned.append(day)
    return pruned

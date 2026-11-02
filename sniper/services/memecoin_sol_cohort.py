"""The Solana cohort stream: our own pre-signal price path, and its 24 h canary.

Spec docs/superpowers/specs/2026-10-09-memecoin-solana-presignal-feed-design.md, step 1. Operator
approved via Alpha, Gamma's five points built in.

MEASUREMENT ONLY, ITS OWN PROCESS (scripts/memecoin_sol_cohort_stream.py). Never inside the funded
runner. Its own key (MEMECOIN_SOL_COHORT_HELIUS_URL, refused when shared with any other configured
URL), its own DB (data/memecoin_sol_cohort.db). The funded ledger is only READ (read-only
connections).

WHAT IT SUBSCRIBES. A coin enters at its first sighting:
- `own`: an armed wallet's on-chain buy in onchain_wallet_detections, with its transaction;
- `j7`: a J7 follow_activity buy in memecoin_events, MEASUREMENT ONLY (Gamma 1). Every coins row and
  every mark carries `trigger_first`, so a later reader can take own-triggered coins alone. J7 is not
  a wallet-signal source (operator 10-01/10-02), and step 1b reads own-triggered coins only unless
  the operator separately allows more.
Each sighting extends the coin's lifetime. An explicit cap evicts the oldest last sighting and counts
it (Gamma 5), so a lifetime is never chosen on a silently truncated set.

HOW. A third instance of the paper vault stream machinery (CohortSolTicks is a PaperSolTicks): the
same resolver, VaultTape/CurveTape stream, per-pool marks, graduation rebind and carry rules. Prices
are in QUOTE units (SOL or USDC per token): SOL/USD lives in the runner process, and phase ratios do
not need USD. The runner-side reader converts.

WHAT THE CANARY MEASURES:
- Received WebSocket bytes, uncompressed, which is how Helius bills, counted at the socket. Credits
  per minute = 2 per 0.1 MB + RPC calls.
- The binding state of every coin (graduation_unresolved is a lost binding, not a quiet pool).
- An epoch timeline (connect, disconnect, error), so a blind stream is never read as flat (Gamma 3).
- Coverage of funded Solana attempts at decision_started_at, per trigger (`coverage`).
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from sniper.services.memecoin_paper_sol_ticks import PaperSolTicks
from sniper.services.memecoin_paper_sol_ticks import key_refusal as _paper_key_refusal

logger = logging.getLogger("memecoin")

KNOB = "MEMECOIN_SOL_COHORT_HELIUS_URL"
LIFETIME_S = 24 * 3600.0          # the canary's upper bound; the plan's lifetime comes from its data
COIN_CAP = 400                    # coins subscribed at once (about 2 accounts each)
POLL_S = 5.0
SAMPLE_S = 15.0
RETAIN_S = 25 * 3600.0
FIRST_SIGHTING_S = 60.0           # a coin first seen this close to a decision had no path before it
SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (cid INTEGER PRIMARY KEY, mint TEXT UNIQUE NOT NULL,
    trigger_first TEXT NOT NULL, j7_first_at REAL, onchain_first_at REAL, onchain_sig TEXT,
    last_seen_at REAL NOT NULL, evicted_at REAL, binding TEXT, binding_at REAL);
CREATE TABLE IF NOT EXISTS marks (mint TEXT NOT NULL, pool TEXT, quote_mint TEXT, ts REAL NOT NULL,
    price_quote REAL NOT NULL, kind TEXT, trigger_first TEXT, PRIMARY KEY (mint, ts));
CREATE INDEX IF NOT EXISTS cohort_marks_ts ON marks(ts);
CREATE TABLE IF NOT EXISTS meter (minute INTEGER PRIMARY KEY, bytes INTEGER, messages INTEGER,
    rpc_calls INTEGER, subscribed INTEGER, credits REAL);
CREATE TABLE IF NOT EXISTS meter_fragments (fragment_id TEXT PRIMARY KEY, minute INTEGER NOT NULL,
    started REAL, ended REAL, elapsed_s REAL, bytes INTEGER NOT NULL, messages INTEGER NOT NULL,
    rpc_calls INTEGER NOT NULL, rpc_methods_json TEXT, rpc_credits REAL,
    unknown_rpc_calls INTEGER NOT NULL, tariff_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS epochs (id INTEGER PRIMARY KEY, started REAL, ended REAL, vaults INTEGER,
    error TEXT, connected_at REAL, connection_observed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cursors (name TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
"""


def key_refusal(url, env):
    """Why `url` may not carry the cohort stream, or None: unset, keyless, option C's key, or the same
    key as ANY other configured variable (a dedicated account, Alpha 2026-10-09)."""
    return _paper_key_refusal(url, env, allowed=(KNOB,))


def connect_db(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    epoch_columns = {r[1] for r in conn.execute("PRAGMA table_info(epochs)")}
    if "connected_at" not in epoch_columns:
        conn.execute("ALTER TABLE epochs ADD COLUMN connected_at REAL")
    if "connection_observed" not in epoch_columns:
        conn.execute("ALTER TABLE epochs ADD COLUMN connection_observed INTEGER NOT NULL DEFAULT 0")
    if "epoch_key" not in epoch_columns:
        conn.execute("ALTER TABLE epochs ADD COLUMN epoch_key TEXT")
    if "observed_until" not in epoch_columns:
        conn.execute("ALTER TABLE epochs ADD COLUMN observed_until REAL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS cohort_epoch_key ON epochs(epoch_key)")
    conn.commit()
    return conn


def _epoch(text):
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _bump(conn, name, by=1):
    conn.execute("INSERT INTO counters VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET "
                 "value = value + excluded.value", (name, int(by)))


def record_sighting(conn, mint, trigger, at, sig=None):
    """One sighting. The first one names `trigger_first`; each one extends the lifetime and revives an
    evicted coin."""
    first_col = "j7_first_at" if trigger == "j7" else "onchain_first_at"
    conn.execute(
        f"INSERT INTO coins (mint, trigger_first, {first_col}, onchain_sig, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(mint) DO UPDATE SET "
        f"{first_col} = COALESCE(coins.{first_col}, excluded.{first_col}), "
        "onchain_sig = COALESCE(coins.onchain_sig, excluded.onchain_sig), "
        "last_seen_at = MAX(coins.last_seen_at, excluded.last_seen_at), evicted_at = NULL",
        (str(mint), trigger, float(at), sig, float(at)))
    conn.commit()


class Triggers:
    """Polls the two sighting sources by id cursor. The first poll starts at the current end (no
    backfill: a coin seen before the stream ran has no path to record)."""

    SOURCES = (
        ("j7", "SELECT id, mint, received_at, NULL FROM memecoin_events WHERE id > ? AND side = 'buy' "
               "AND network_id = 'solana' ORDER BY id", "memecoin_events"),
        ("own", "SELECT id, mint, detected_at, signature FROM onchain_wallet_detections WHERE id > ? "
                "AND network_id = 'solana' ORDER BY id", "onchain_wallet_detections"),
    )

    def __init__(self, store, ledger_factory):
        self.store, self.ledger_factory = store, ledger_factory
        self._ledger = None              # one read-only connection (autocommit: pins no WAL)

    def poll(self):
        if self._ledger is None:
            self._ledger = self.ledger_factory()
        ledger, n = self._ledger, 0
        for trigger, query, table in self.SOURCES:
            row = self.store.execute("SELECT last_id FROM cursors WHERE name = ?", (table,)).fetchone()
            if row is None:
                top = ledger.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
                self.store.execute("INSERT INTO cursors VALUES (?, ?)", (table, int(top)))
                continue
            last = int(row[0])
            # The end read BEFORE the rows: every id up to it is in the query below, and a row that
            # lands after it is either in the query too (its id is then the new cursor) or next pass.
            top = int(ledger.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])
            seen = last
            for rid, mint, at, sig in ledger.execute(query, (last,)).fetchall():
                seen = max(seen, int(rid))
                when = _epoch(at)
                if mint and when is not None:
                    record_sighting(self.store, mint, trigger, when, sig=sig)
                    n += 1
            self.store.execute("UPDATE cursors SET last_id = ? WHERE name = ?",
                               (max(last, top, seen), table))
            self.store.commit()
        return n


def wanted(conn, now, *, lifetime_s=LIFETIME_S, cap=COIN_CAP):
    """The coins to subscribe now: alive (a sighting inside the lifetime), not evicted, newest last
    sighting first. Past the cap the oldest are evicted and counted (Gamma 5)."""
    # OWN-SIGHTED COINS FIRST (Gamma LOW 1): step 1b reads the own population, so the cap spends J7-only
    # coins (onchain_first_at NULL) before any coin an armed wallet bought.
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM coins WHERE evicted_at IS NULL AND last_seen_at >= ? "
        "ORDER BY (onchain_first_at IS NULL), last_seen_at DESC, cid",
        (float(now) - float(lifetime_s),))]
    if len(rows) > int(cap):
        evict = rows[int(cap):]
        conn.executemany("UPDATE coins SET evicted_at = ? WHERE cid = ?",
                         [(float(now), r["cid"]) for r in evict])
        j7_only = sum(1 for r in evict if r["onchain_first_at"] is None)
        _bump(conn, "cap_hits")
        _bump(conn, "evictions", len(evict))
        _bump(conn, "evictions:j7_only", j7_only)
        _bump(conn, "evictions:own", len(evict) - j7_only)
        conn.commit()
        rows = rows[:int(cap)]
    return rows


def origin_of(row):
    """An own sighting binds from its transaction (the resolver's `source_swap` path)."""
    sig = row.get("onchain_sig")
    return "swap:raybot:%s:" % sig if sig else None


def candidates(ledger, mint):
    """Pools to try for a coin with no transaction (a J7 sighting): the latest paper position's own
    binding on the coin (zero reads to find), then pump's bonding curve and canonical PumpSwap pool,
    both derived. Each is resolved by the stream's resolver; the first that binds wins."""
    out = []
    try:
        row = ledger.execute(
            "SELECT paper_tick_json FROM memecoin_positions WHERE mint = ? AND network_id = 'solana' "
            "AND execution_mode = 'paper' AND paper_tick_json IS NOT NULL ORDER BY opened_at DESC "
            "LIMIT 1", (str(mint),)).fetchone()
        bound = json.loads(row[0]) if row else {}
        if isinstance(bound, dict) and bound.get("pool") and bound.get("binding") != "unbound":
            out.append(str(bound["pool"]))
    except (sqlite3.Error, TypeError, ValueError):
        logger.exception("cohort: paper binding unreadable mint=%s", mint)
    try:
        from sniper.execution.solana_native_entry_quote import accounts_for
        derived = accounts_for(mint)
        out += [derived["curve"], derived["pool"]]
    except Exception:
        pass                                   # not a valid base58 key: no derived pool
    return list(dict.fromkeys(out))


class CohortSolTicks(PaperSolTicks):
    """The paper vault stream's machinery on the cohort key, priced in quote units."""

    def __init__(self, url, *, ledger_factory, **kwargs):
        kwargs.setdefault("quote_usd_fn", lambda mints: {m: 1.0 for m in mints})
        super().__init__(url, **kwargs)
        self.ledger_factory = ledger_factory
        self.last_seen = {}              # cid -> latest sighting (epoch s), set by the main loop
        self._origins = {}               # cid -> the transaction origin last offered

    def resolve_pass(self):
        """A NEW TRANSACTION RE-OPENS BINDING (canary hour 1, 2026-10-09): a coin J7 sighted first has
        no transaction, can spend its tries on the candidates and stay unbound. The armed wallet's later
        on-chain buy brings the source transaction, the best binding there is, so an unbound coin
        offered a new origin gets its tries back. A bound coin keeps its binding."""
        with self._lock:
            for pid, _mint, _pool, origin in list(self.wanted):
                if origin and self._origins.get(pid) != origin:
                    self._origins[pid] = origin
                    if pid not in self._bindings and self._attempts.pop(pid, None):
                        self.counters["retry_on_own_origin"] += 1
            for pid in [p for p in self._origins if p not in {w[0] for w in self.wanted}]:
                self._origins.pop(pid, None)
        super().resolve_pass()

    def _on_row(self, row):
        """Count each row by time since its coin's latest sighting, matching renewed retention."""
        super()._on_row(row)
        from sniper.services.memecoin_paper_sol_ticks import stream_pool
        pool = str(row.get("pool") or "")
        with self._lock:
            # ponytail: a scan of the bound coins per row (~400); a pool->cid map if the CPU shows it
            cid = next((pid for pid, b in self._bindings.items() if stream_pool(b) == pool), None)
            last = self.last_seen.get(cid) if cid is not None else None
        if last is None:
            bucket = "unknown"
        else:
            age = self.clock() - float(last)
            bucket = "lt1h" if age <= 3600 else "1to6h" if age <= 6 * 3600 else "6to24h"
        self.counters["rows_age:%s" % bucket] += 1

    def _resolve(self, mint, pool, origin):
        if pool or origin:
            return super()._resolve(mint, pool, origin)
        ledger = self.ledger_factory()
        try:
            pools = candidates(ledger, mint)
        finally:
            with contextlib.suppress(Exception):
                ledger.close()
        failed = False
        for cand in pools:
            kind, got = super()._resolve(mint, cand, None)
            if got:
                self.counters["bound_candidate:%d" % pools.index(cand)] += 1
                return kind, got
            failed = failed or kind == "read_failed"
        return ("read_failed" if failed else None), None

    def binding(self, pid):
        got = super().binding(pid)
        if got and got.get("pool"):
            with self._lock:
                entry = self._bindings.get(int(pid))
            if entry:
                got = dict(got, quote_mint=entry.get("quote_mint"))
        return got


def sample_once(conn, src, coins, now):
    """One mark per coin with a FRESH tick, in quote units. No tick writes nothing (a quiet pool and a
    dead stream are told apart by the epoch timeline, not by a carried row)."""
    n = 0
    for row in coins:
        tick, _why = src.tick({"id": row["cid"], "mint": row["mint"]}, None, now)
        if tick is None:
            continue
        bound = src.binding(row["cid"]) or {}
        conn.execute(
            "INSERT OR IGNORE INTO marks (mint, pool, quote_mint, ts, price_quote, kind, trigger_first) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["mint"], bound.get("pool"), bound.get("quote_mint"), float(now), float(tick.price),
             tick.tick_kind, row["trigger_first"]))
        n += 1
    conn.commit()
    return n


def sync_bindings(conn, src, coins, now):
    """Each coin's binding state, kept, so a lost binding (graduation_unresolved) is counted."""
    for row in coins:
        got = src.binding(row["cid"]) or {}
        conn.execute("UPDATE coins SET binding = ?, binding_at = ? WHERE cid = ?",
                     (got.get("binding") or "pending", float(now), row["cid"]))
    conn.commit()


def binding_summary(conn):
    return dict(conn.execute("SELECT binding, COUNT(*) FROM coins WHERE binding IS NOT NULL "
                             "GROUP BY binding").fetchall())


def snapshot_counters(conn, counters):
    """The stream's own counters (graduations, rebinds, grad_unresolved, rpc calls, rows by age...)
    as `src:<name>` rows, overwritten each minute."""
    conn.execute("DELETE FROM counters WHERE name LIKE 'src:%'")
    conn.executemany("INSERT OR REPLACE INTO counters VALUES (?, ?)",
                     [("src:%s" % k, int(v)) for k, v in dict(counters).items()])
    conn.commit()


def prune(conn, now):
    conn.execute("DELETE FROM marks WHERE ts < ?", (float(now) - RETAIN_S,))
    conn.execute("DELETE FROM epochs WHERE ended < ?", (float(now) - 7 * 86400.0,))
    conn.commit()


class ByteMeter:
    """Received WebSocket bytes and messages since the last `take()` (thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._bytes = self._messages = 0

    def add(self, n):
        with self._lock:
            self._bytes += int(n)
            self._messages += 1

    def take(self):
        with self._lock:
            out, self._bytes, self._messages = (self._bytes, self._messages), 0, 0
        return out


class _Counted:
    def __init__(self, ws, meter):
        self._ws, self._meter = ws, meter

    async def send(self, data):
        return await self._ws.send(data)

    def __aiter__(self):
        async def gen():
            async for raw in self._ws:
                self._meter.add(len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw))
                yield raw
        return gen()

    def __getattr__(self, name):
        return getattr(self._ws, name)


def counted_connect(connect, meter):
    """`connect` with every received message metered at its uncompressed size (Helius bills that)."""
    @contextlib.asynccontextmanager
    async def _connect(url):
        async with connect(url) as ws:
            yield _Counted(ws, meter)
    return _connect


RPC_TARIFF_VERSION = "helius-rpc-2026-10-10-v2"
# https://www.helius.dev/docs/billing/credits: getTransaction/getBlock 1, getProgramAccounts 10.
# An unlisted method remains unknown; adding a premium method cannot silently price it at one.
RPC_CREDITS = {method: 1 for method in (
    'getAccountInfo', 'getMultipleAccounts', 'getTokenAccountsByOwner', 'getTokenAccountBalance',
    'getBalance', 'getSignaturesForAddress', 'getLatestBlockhash', 'getSlot', 'getBlockHeight',
    'getSignatureStatuses', 'getProgramAccounts', 'getMinimumBalanceForRentExemption')}
RPC_CREDITS.update(getTransaction=1, getBlock=1, getProgramAccounts=10)


def record_minute(conn, *, minute, bytes_, messages, rpc_calls, subscribed,
                  fragment_id=None, started=None, ended=None, rpc_method_counts=None):
    """One immutable flush; retries reuse its ID, restarts use another ID. Raw counts survive.

    Legacy callers retain their raw minute total, but their tariff/elapsed observation is unknown.
    New production flushes supply method deltas and exact interval bounds.
    """
    methods = None if rpc_method_counts is None else dict(rpc_method_counts)
    if methods is not None and (any(type(v) is not int or v < 0 for v in methods.values())
                                or sum(methods.values()) != rpc_calls):
        raise ValueError('RPC method counts do not match raw requests')
    known = sum(v * RPC_CREDITS[k] for k, v in (methods or {}).items() if k in RPC_CREDITS)
    unknown = (rpc_calls if methods is None else
               sum(v for k, v in methods.items() if k not in RPC_CREDITS))
    rpc_credits = float(known)  # known component; unknown calls are retained separately
    elapsed = None if started is None or ended is None else float(ended) - float(started)
    if elapsed is not None and elapsed < 0:
        raise ValueError('meter interval reversed')
    # Preserve old raw credits for compatibility; reports flag their missing tariff provenance.
    credits = float(bytes_) / 100_000.0 * 2.0 + (float(rpc_calls) if methods is None else known)
    if unknown and methods is not None:
        credits = None
    with conn:
        inserted = conn.execute(
            'INSERT OR IGNORE INTO meter_fragments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (fragment_id or uuid.uuid4().hex, int(minute), started, ended, elapsed, int(bytes_),
             int(messages), int(rpc_calls), None if methods is None else json.dumps(methods, sort_keys=True),
             rpc_credits, int(unknown), RPC_TARIFF_VERSION)).rowcount
        if inserted:
            conn.execute('INSERT INTO meter VALUES (?,?,?,?,?,?) ON CONFLICT(minute) DO UPDATE SET '
                         'bytes=meter.bytes+excluded.bytes, messages=meter.messages+excluded.messages, '
                         'rpc_calls=meter.rpc_calls+excluded.rpc_calls, subscribed=excluded.subscribed, '
                         'credits=meter.credits+excluded.credits',
                         (int(minute), int(bytes_), int(messages), int(rpc_calls), int(subscribed), credits))


def record_epoch(conn, *, started, ended, vaults, error, connected_at=None, connection_observed=False,
                 epoch_key=None, observed_until=None):
    """A missing connection timestamp is known blind only when the connection was observed."""
    conn.execute("INSERT INTO epochs (started, ended, vaults, error, connected_at, connection_observed, epoch_key, observed_until) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(epoch_key) DO UPDATE SET "
                 "ended=excluded.ended, error=excluded.error, connected_at=excluded.connected_at, "
                 "connection_observed=excluded.connection_observed, observed_until=excluded.observed_until",
                 (float(started), float(ended) if ended is not None else None, int(vaults), error,
                  float(connected_at) if connected_at is not None else None, int(connection_observed), epoch_key,
                  observed_until if observed_until is not None else ended))
    conn.commit()


def coverage(ledger, conn, *, since, until, window_s=900.0, min_points=3, min_span_s=30.0):
    """Funded-lane Solana attempts in [since, until), measured AT the decision (Gamma 2):
    - covered[T]: the coin had >= min_points marks over >= min_span_s in [t - window_s, t), counting
      only marks after trigger T's own first sighting (what trigger T alone would have held);
    - first_sighting: the coin was first seen within FIRST_SIGHTING_S of the decision, so it is
      structurally uncoverable;
    - never_sighted: no sighting at all."""
    out = {"attempts": 0, "covered": {"own": 0, "j7": 0}, "first_sighting": 0, "never_sighted": 0}
    for (mint, at) in ledger.execute(
            "SELECT mint, decision_started_at FROM memecoin_entry_attempts WHERE network_id = 'solana' "
            "AND decision_started_at >= ? AND decision_started_at < ? "
            "AND COALESCE(delivery_lane, '') NOT LIKE '%shadow%'", (since, until)).fetchall():
        t = _epoch(at)
        if t is None:
            continue
        out["attempts"] += 1
        coin = conn.execute("SELECT * FROM coins WHERE mint = ?", (mint,)).fetchone()
        if coin is None:
            out["never_sighted"] += 1
            continue
        firsts = {"own": coin["onchain_first_at"], "j7": coin["j7_first_at"]}
        first = min(v for v in firsts.values() if v is not None)
        if first >= t - FIRST_SIGHTING_S:
            out["first_sighting"] += 1
        ts = [r[0] for r in conn.execute("SELECT ts FROM marks WHERE mint = ? AND ts >= ? AND ts < ? "
                                         "ORDER BY ts", (mint, t - window_s, t))]
        for trigger, first_at in firsts.items():
            mine = [x for x in ts if first_at is not None and x >= first_at]
            if len(mine) >= min_points and mine[-1] - mine[0] >= min_span_s:
                out["covered"][trigger] += 1
    return out


# ---- the canary's verdict ------------------------------------------------------------------------

PLANS = (("Free", 0, 1_000_000), ("Developer", 49, 10_000_000), ("Business", 499, 100_000_000),
         ("Professional", 999, 200_000_000))         # Helius billing docs, read 2026-10-09


def lifetime_projection(counters, *, stream_credits_month, rpc_credits_month):
    """Monthly credits per lifetime choice: the canary ran the 24 h upper bound, and a shorter
    lifetime keeps rows within that lifetime of the latest sighting (rows_age buckets). RPC credits
    (binding and seed reads) do not shrink with the lifetime."""
    rows = {k: float(counters.get("src:rows_age:%s" % k, 0) or 0) for k in ("lt1h", "1to6h", "6to24h")}
    total = sum(rows.values()) or 1.0
    share = {"1h": rows["lt1h"] / total, "6h": (rows["lt1h"] + rows["1to6h"]) / total, "24h": 1.0}
    return {name: float(stream_credits_month) * frac + float(rpc_credits_month)
            for name, frac in share.items()}


def plan_for(credits_month):
    """(plan, $/month) of the cheapest plan whose monthly credits hold `credits_month`."""
    for name, usd, credits in PLANS:
        if float(credits_month) <= credits:
            return name, usd
    return None, None


# ---- the process --------------------------------------------------------------------------------

def run(env, *, store_path=None, ledger_path=None, marker_path=None, sleep=time.sleep,
        clock=time.time):
    """The cohort process. Returns 0 at once when the key guard refuses (the knob unset: default off).

    A REFUSED KEY LEAVES A MARKER (Gamma LOW 2): with the knob set but the key refused (keyless, option
    C's, or shared), the process writes `marker_path` (the reason, never the key) and exits 0. The
    watchdog honours a marker newer than .env, so it does not relaunch every minute; editing .env
    clears it. An unset knob is plain off, and a start removes any marker."""
    import os

    from sniper.execution.solana_rpc_pool import key_id
    from sniper.util.state_paths import state_path
    marker = marker_path or str(state_path("data/memecoin_sol_cohort.refused"))
    url = str(env.get(KNOB) or "").strip()
    why = key_refusal(url, env)
    if why is not None:
        logger.warning("sol cohort: NOT started (%s)%s", why,
                       "" if why in ("unset", "keyless") else " (key %s)" % key_id(url))
        if why == "unset":
            with contextlib.suppress(FileNotFoundError):
                os.remove(marker)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(marker)), exist_ok=True)
            with open(marker, "w") as fh:
                fh.write("%s\n" % why)
        return 0
    with contextlib.suppress(FileNotFoundError):
        os.remove(marker)
    from sniper.persistence.db_core import get_db_path, get_readonly_connection
    from sniper.util.state_paths import state_path
    store = connect_db(store_path or str(state_path("data/memecoin_sol_cohort.db")))
    ledger_factory = functools.partial(get_readonly_connection, ledger_path or get_db_path())
    meter, epochs = ByteMeter(), []
    stream = _metered_stream(meter, epochs, clock)
    src = CohortSolTicks(url, ledger_factory=ledger_factory, stream_fn=stream)
    threading.Thread(target=src.resolve_forever, name="sol-cohort-bind", daemon=True).start()
    threading.Thread(target=src.stream_forever, name="sol-cohort-stream", daemon=True).start()
    logger.info("sol cohort: started on key %s", key_id(url))
    triggers = Triggers(store, ledger_factory)
    last_sample = last_prune = 0.0
    last_minute = clock()
    rpc_seen, pending_fragment = {}, None
    run_id, sequence = uuid.uuid4().hex, 0
    while True:
        now = clock()
        try:
            triggers.poll()
            coins = wanted(store, now)
            with src._lock:
                src.last_seen = {int(r["cid"]): r["last_seen_at"] for r in coins}
                src.wanted = [(int(r["cid"]), r["mint"], None, origin_of(r)) for r in coins]
            if now - last_sample >= SAMPLE_S:
                last_sample = now
                sample_once(store, src, coins, now)
                sync_bindings(store, src, coins, now)
            if pending_fragment is not None or now - last_minute >= 60.0:
                if pending_fragment is None:
                    rpc = {k[4:]: v for k, v in dict(src.counters).items() if k.startswith('rpc:')}
                    methods = {k: v - rpc_seen.get(k, 0) for k, v in rpc.items()}
                    bytes_, messages = meter.take()
                    sequence += 1
                    pending_fragment = dict(minute=int(last_minute // 60), bytes_=bytes_, messages=messages,
                        rpc_calls=sum(methods.values()), subscribed=src.counters.get('subscribed', 0),
                        rpc_method_counts=methods, fragment_id='%s:%s' % (run_id, sequence),
                        started=last_minute, ended=now)
                record_minute(store, **pending_fragment)
                rpc_seen = {k: rpc_seen.get(k, 0) + v for k, v in pending_fragment['rpc_method_counts'].items()}
                last_minute = pending_fragment['ended']
                pending_fragment = None
                snapshot_counters(store, src.counters)
            for epoch in list(epochs):
                snapshot = dict(epoch)
                if snapshot['ended'] is None:
                    snapshot['observed_until'] = now
                record_epoch(store, **snapshot)
                if snapshot['ended'] is not None:
                    epochs.remove(epoch)
            if now - last_prune >= 3600.0:
                last_prune = now
                prune(store, now)
        except Exception:
            logger.exception("sol cohort: pass failed")
        sleep(POLL_S)


def _metered_stream(meter, epochs, clock):
    """Meter each attempt and its connected interval, excluding handshake and close cleanup.
    The main loop writes the epochs; its store connection belongs to the main thread."""
    import websockets

    from sniper.services.memecoin_paper_sol_ticks import PING_S
    from sniper.services.memecoin_solana_ws import run_vault_stream
    connect = counted_connect(functools.partial(websockets.connect, ping_interval=PING_S,
                                                ping_timeout=PING_S), meter)

    async def stream(vaults, on_row, **kwargs):
        started, error = clock(), None
        connected_at = ended = None
        epoch = dict(started=started, ended=None, vaults=len(vaults), error=None,
                     connected_at=None, connection_observed=True, epoch_key=uuid.uuid4().hex,
                     observed_until=started)
        epochs.append(epoch)

        @contextlib.asynccontextmanager
        async def epoch_connect(url):
            nonlocal connected_at, ended
            async with connect(url) as ws:
                connected_at = clock()
                epoch['connected_at'] = connected_at
                epoch['observed_until'] = connected_at
                try:
                    yield ws
                finally:
                    ended = clock()

        try:
            return await run_vault_stream(vaults, on_row, connect=epoch_connect, **kwargs)
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            final = clock()
            epoch.update(ended=final if ended is None else ended, error=error, observed_until=final)
    return stream

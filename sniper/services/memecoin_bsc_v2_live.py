"""BSC V2 volume and V2/V3 funded-pool marks, observed off the order path."""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sniper.services.memecoin_tick_stream import (
    PANCAKE_V3_SWAP, V2_SWAP, V2_SYNC, V3_SWAP, decode_evm_log)
from sniper.util.clock import real_utc_now
from sniper.util.state_paths import state_root

CHAIN = "56"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
RPC = "https://bsc-rpc.publicnode.com"
WS = "wss://bsc-rpc.publicnode.com"
PRICE_TTL_S = 300.0
HEAD_TTL_S = 3.0
DELIVERY_MAX_LAG_S = 3.0
QUOTE_TTL_S = 60.0
HOP_TARGET_CAP = 128
HOP_PIN_REFRESH_S = 300.0         # F1b: funded-route quotes re-read from the ledger every 5 min
# A SUBSCRIPTION ID IS NOT DELIVERY (2026-10-14): publicnode's PairCreated stream (id 6) went silent
# at 15:23:00Z while heads, Swaps and Syncs flowed, and the stall check below never covered it: no
# new hop targets and no presignal safety candidates for over an hour. PCS V2 pairs arrive every
# 1.3 s at p50, 22 s at p99 (measured 15:15-15:23Z); past this silence the socket is reset.
PAIR_CREATED_SILENT_S = 120.0
# An EXPLICIT rejection of the subscription keeps the core tape (as before) and retries on this slower
# clock (Gamma MEDIUM on 9e77b420a): a persistent refusal resets 4 times an hour, not 30, while a
# provider-wide bad patch (10-12 13:20Z, 13:39Z: every subscription refused) still recovers.
PAIR_CREATED_REJECTED_RETRY_S = 900.0
PCS_V2_FACTORY = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"
PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"


def db_path():
    return state_root() / "data/memecoin_bsc_v2_live.db"


def connect(path=None, *, readonly=False):
    path = Path(path or db_path()).resolve()
    if readonly:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=0.1)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS feed (
                id INTEGER PRIMARY KEY CHECK(id=1), since REAL, height INTEGER,
                hash TEXT, head_ts REAL, received REAL);
            INSERT OR IGNORE INTO feed(id) VALUES (1);
            CREATE TABLE IF NOT EXISTS heads (
                height INTEGER PRIMARY KEY, hash TEXT, block_ts REAL);
            CREATE TABLE IF NOT EXISTS head_parents (
                height INTEGER PRIMARY KEY, parent_hash TEXT);
            CREATE TABLE IF NOT EXISTS swaps (
                pool TEXT, height INTEGER, hash TEXT, tx TEXT, idx INTEGER,
                amount0 TEXT, amount1 TEXT, PRIMARY KEY(pool,tx,idx));
            CREATE INDEX IF NOT EXISTS bsc_swaps_pool_height ON swaps(pool,height);
            CREATE INDEX IF NOT EXISTS bsc_swaps_height ON swaps(height);
            CREATE TABLE IF NOT EXISTS syncs (
                pool TEXT PRIMARY KEY, height INTEGER, hash TEXT, idx INTEGER,
                reserve0 TEXT, reserve1 TEXT, received REAL);
            CREATE TABLE IF NOT EXISTS sync_sources (
                pool TEXT PRIMARY KEY, tx TEXT, height INTEGER, idx INTEGER);
            CREATE TABLE IF NOT EXISTS pools (
                pool TEXT PRIMARY KEY, token0 TEXT, token1 TEXT, mint_decimals INTEGER);
            CREATE TABLE IF NOT EXISTS v3_pools (
                pool TEXT PRIMARY KEY, token0 TEXT, token1 TEXT, mint_decimals INTEGER);
            CREATE TABLE IF NOT EXISTS v3_marks (
                pool TEXT PRIMARY KEY, height INTEGER, hash TEXT, idx INTEGER,
                sqrt_price_x96 TEXT, received REAL);
            CREATE TABLE IF NOT EXISTS quote (
                id INTEGER PRIMARY KEY CHECK(id=1), usd REAL, observed REAL);
            CREATE TABLE IF NOT EXISTS hop_targets (
                pool TEXT PRIMARY KEY, quote_token TEXT NOT NULL, observed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS hop_pins (
                pool TEXT PRIMARY KEY, quote_token TEXT NOT NULL, observed REAL NOT NULL);
        """)
    c.row_factory = sqlite3.Row
    return c


def reset_coverage(c):
    c.execute("UPDATE feed SET since=NULL,height=NULL,hash=NULL,head_ts=NULL,received=NULL WHERE id=1")
    c.execute("DELETE FROM heads")
    c.execute("DELETE FROM head_parents")
    c.execute("DELETE FROM swaps")
    c.execute("DELETE FROM syncs")
    c.execute("DELETE FROM sync_sources")
    c.execute("DELETE FROM v3_marks")
    c.commit()


def record_head(c, height, block_hash, block_ts, received, parent_hash=None):
    height, block_ts, received = int(height), float(block_ts), float(received)
    block_hash = str(block_hash).lower()
    previous = c.execute("SELECT height,hash,since FROM feed WHERE id=1").fetchone()
    known = c.execute("SELECT hash FROM heads WHERE height=?", (height,)).fetchone()
    if (known and known["hash"] != block_hash) or (
            previous["height"] is not None and height > previous["height"] + 16) or (
            parent_hash and previous["height"] == height - 1
            and str(parent_hash).lower() != previous["hash"]):
        reset_coverage(c)
        previous = c.execute("SELECT height,hash,since FROM feed WHERE id=1").fetchone()
    c.execute("INSERT OR REPLACE INTO heads VALUES (?,?,?)", (height, block_hash, block_ts))
    c.execute("INSERT OR REPLACE INTO head_parents VALUES (?,?)",
              (height, str(parent_hash).lower() if parent_hash else None))
    if previous["height"] is None:
        c.execute("UPDATE feed SET since=COALESCE(since,?),height=?,hash=?,head_ts=?,received=? WHERE id=1",
                  (block_ts, height, block_hash, block_ts, received))
    elif height == previous["height"] + 1:
        frontier = height
        while c.execute("SELECT 1 FROM heads WHERE height=?", (frontier + 1,)).fetchone():
            current_hash = c.execute("SELECT hash FROM heads WHERE height=?", (frontier,)).fetchone()[0]
            next_parent = c.execute("SELECT parent_hash FROM head_parents WHERE height=?",
                                    (frontier + 1,)).fetchone()
            if next_parent and next_parent[0] and next_parent[0] != current_hash:
                reset_coverage(c)
                c.execute("INSERT OR REPLACE INTO heads VALUES (?,?,?)", (height, block_hash, block_ts))
                c.execute("INSERT OR REPLACE INTO head_parents VALUES (?,?)", (height, parent_hash))
                c.execute("UPDATE feed SET since=?,height=?,hash=?,head_ts=?,received=? WHERE id=1",
                          (block_ts, height, block_hash, block_ts, received))
                c.commit()
                return
            frontier += 1
        top = c.execute("SELECT hash,block_ts FROM heads WHERE height=?", (frontier,)).fetchone()
        c.execute("UPDATE feed SET height=?,hash=?,head_ts=?,received=? WHERE id=1",
                  (frontier, top["hash"], top["block_ts"], received))
    elif height > previous["height"] + 1:
        last = c.execute("SELECT received FROM feed WHERE id=1").fetchone()[0]
        if last is not None and received - last > 3:
            reset_coverage(c)
            c.execute("INSERT OR REPLACE INTO heads VALUES (?,?,?)", (height, block_hash, block_ts))
            c.execute("INSERT OR REPLACE INTO head_parents VALUES (?,?)", (height, parent_hash))
            c.execute("UPDATE feed SET since=?,height=?,hash=?,head_ts=?,received=? WHERE id=1",
                      (block_ts, height, block_hash, block_ts, received))
    c.execute("DELETE FROM heads WHERE height<?", (height - 900,))
    c.execute("DELETE FROM head_parents WHERE height<?", (height - 900,))
    c.execute("DELETE FROM swaps WHERE height<?", (height - 900,))
    c.commit()


def record_swap(c, pool, height, block_hash, tx, idx, amount0, amount1):
    c.execute("INSERT OR IGNORE INTO swaps VALUES (?,?,?,?,?,?,?)",
              (str(pool).lower(), int(height), str(block_hash).lower(), str(tx).lower(),
               int(idx), str(amount0), str(amount1)))
    c.commit()


def record_sync(c, pool, height, block_hash, reserve0, reserve1, received, idx=0, tx=None):
    c.execute("INSERT INTO syncs VALUES (?,?,?,?,?,?,?) ON CONFLICT(pool) DO UPDATE SET "
              "height=excluded.height,hash=excluded.hash,idx=excluded.idx,"
              "reserve0=excluded.reserve0,reserve1=excluded.reserve1,received=excluded.received "
              "WHERE excluded.height>syncs.height OR "
              "(excluded.height=syncs.height AND excluded.idx>=syncs.idx)",
              (str(pool).lower(), int(height), str(block_hash).lower(), int(idx),
               str(reserve0), str(reserve1), float(received)))
    stored = c.execute("SELECT height,hash,idx FROM syncs WHERE pool=?",
                       (str(pool).lower(),)).fetchone()
    if tx and stored and (stored["height"], stored["hash"], stored["idx"]) == (
            int(height), str(block_hash).lower(), int(idx)):
        c.execute("INSERT OR REPLACE INTO sync_sources VALUES (?,?,?,?)",
                  (str(pool).lower(), str(tx).lower(), int(height), int(idx)))
    c.commit()


# THE BSC FAST LANE'S HOP PAIRS (Alpha 2026-10-02). The WBNB pairs of the configured meme quotes are
# recorded like a held pool while the lane runs (MEMECOIN_BSC_FAST_LANE runner_shadow|live; off means
# no hop pair and no seed call), and seeded once per coverage window with getReserves, so a quiet pair
# has a state before it next trades. A pair that fails is retried later in the same window at the
# then-current head (Alpha/Gamma on 8423374ac); any height at or after the window start is valid.
# THE SEED HOST (Alpha 2026-10-02): the operator's keyed dRPC 56 account, through its one
# cross-process pacer (memecoin_research_pacer.drpc_paced), shared with the exit tape and the 56
# research reads. 1rpc.io/bnb answered -32001 "usage limit" on every call. Unset: no seed.
HOP_SEED_URL_VAR = "MEMECOIN_RESEARCH_DRPC_RPC_URL_56"
HOP_SEED_GAP_S = 1.05
HOP_SEED_TIMEOUT_S = 8.0
HOP_SEED_LAG_BLOCKS = 2            # 1rpc can trail the tape's head; the seed reads a little behind it
HOP_SEED_BACKOFF_S = (2.0, 5.0, 15.0, 30.0, 60.0)


def records_sync(pool, *, held, hops):
    """A V2 Sync the tape records: a held position's pool or a lane hop pair."""
    return pool in held or pool in hops


def tape_hop_pairs():
    """The lane's hop pairs while the lane runs, else none (no hop recording, no seed call)."""
    from sniper.services.memecoin_bsc_fast_lane import hop_pairs
    from sniper.settings import memecoin as cfg
    return hop_pairs() if cfg.memecoin_bsc_fast_lane_mode() != "off" else {}


def created_hop_targets(log):
    """PCS factory creation -> possible WBNB hop READ TARGETS, never reserve facts.
    Warm both sides: a newly launched coin can itself become the next coin's quote."""
    from sniper.services.memecoin_bsc_fast_lane import our_pair
    try:
        topics, data = log.get("topics") or (), str(log.get("data") or "").lower()
        if (str(log.get("address") or "").lower() != PCS_V2_FACTORY or log.get("removed")
                or len(topics) != 3 or str(topics[0]).lower() != PAIR_CREATED
                or not re.fullmatch(r"0x[0-9a-f]{128}", data)):
            return {}
        if not all(re.fullmatch(r"0x0{24}[0-9a-fA-F]{40}", t) for t in topics[1:]):
            return {}
        a, b = ("0x" + t[-40:].lower() for t in topics[1:])
        if int(a, 16) <= 0 or int(a, 16) >= int(b, 16) or data[2:26] != "0" * 24:
            return {}
        if "0x" + data[26:66] != our_pair(a, b):
            return {}
        return {our_pair(WBNB, q): q for q in (a, b) if q != WBNB}
    except (TypeError, ValueError):
        return {}


def created_native_candidates(log, observed_at):
    """Reuse the verified factory/CREATE2 facts as native read targets; no provider/DB lookup."""
    if not created_hop_targets(log):
        return []
    from sniper.services.memecoin_bsc_fast_lane import candidate_refusal
    a, b = ("0x" + topic[-40:].lower() for topic in log["topics"][1:])
    pool = "0x" + log["data"][26:66].lower()
    out = []
    for mint, quote in ((a, b), (b, a)):
        candidate = {"message_kind": "candidate_v1", "chain": CHAIN, "mint": mint,
                     "quote": quote, "pool": pool, "observed_at": observed_at}
        if candidate_refusal(candidate, now=observed_at) is None:
            out.append(candidate)
    return out


def publish_native_candidates(log, observed_at, *, address=None, send_fn=None):
    from sniper.services.memecoin_bsc_fast_lane import default_address, send_signal, presignal_warm_enabled
    if not presignal_warm_enabled():
        return 0
    address, send_fn = address or default_address(), send_fn or send_signal
    return sum(bool(send_fn(address, candidate))
               for candidate in created_native_candidates(log, observed_at))


def route_hop_targets(routes):
    """Existing routes' quote tokens are restart read targets; no roster or funded authority: a mixed
    route's base_token, and (F1c) the coin-side quote path[-2] of a PancakeSwap V2 router path, the
    only quote the lane's WBNB -> quote -> coin hop reads."""
    from sniper.services.memecoin_bsc_fast_lane import PCS_V2_ROUTER, our_pair
    out = {}
    for raw in routes:
        try:
            route = json.loads(raw or "{}")
            path = route.get("path")
            if route.get("protocol") == "pcs_mixed_v3_v2":
                quote = str(route.get("base_token") or "").lower()
            elif (route.get("protocol") == "uniswap_v2" and isinstance(path, list) and len(path) >= 3
                    and str(route.get("router") or "").lower() == PCS_V2_ROUTER):
                quote = str(path[-2]).lower()
            else:
                continue
            if quote != WBNB and re.fullmatch(r"0x[0-9a-f]{40}", quote) and int(quote, 16) > 0:
                out[our_pair(WBNB, quote)] = quote
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def remember_hop_targets(c, targets, now):
    c.executemany("INSERT INTO hop_targets VALUES (?,?,?) ON CONFLICT(pool) DO UPDATE SET "
                  "quote_token=excluded.quote_token,observed=excluded.observed",
                  [(p, q, float(now)) for p, q in targets.items()])
    c.execute("DELETE FROM hop_targets WHERE pool NOT IN "
              "(SELECT pool FROM hop_targets ORDER BY observed DESC,pool LIMIT ?)", (HOP_TARGET_CAP,))
    c.commit()


def remember_hop_pins(c, pins, now):
    """F1b: the pinned hop pairs (funded-route quotes) replace the previous set. Never evicted by the
    PairCreated targets' cap."""
    c.execute("DELETE FROM hop_pins")
    c.executemany("INSERT INTO hop_pins VALUES (?,?,?)", [(p, q, float(now)) for p, q in pins.items()])
    c.commit()


def read_route_pins():
    """Hop read targets from the last 256 real routes and 256 submitted native attempts.
    Failed submissions also used their hop. An incomplete read keeps the previous pins."""
    from sniper.persistence import db_core
    from sniper.services.memecoin_bsc_fast_lane import _fixed_hex, our_pair, wanted_hop_quotes
    ledger = native = None
    try:
        ledger = db_core.get_readonly_connection()
        ledger.execute("PRAGMA busy_timeout=100")
        pins = route_hop_targets(r[0] for r in ledger.execute(
            "SELECT evm_route_json FROM memecoin_positions WHERE network_id='56' "
            "AND execution_mode='real' ORDER BY id DESC LIMIT 256"))
        # Quotes an armed buy needed but the tape did not track (the watcher's wanted list).
        for quote in wanted_hop_quotes(now=time.time(),
                                       path=state_root() / "data" / "bsc_hop_wanted.txt"):
            pins.setdefault(our_pair(WBNB, quote), quote)
        path = state_root() / "data" / "memecoin_bsc_fast_lane.db"
        try:
            path.stat()
        except FileNotFoundError:
            return pins
        native = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)
        if native.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bsc_lane_live'").fetchone() is None:
            return pins                      # the live table is lazy until the first native attempt
        rows = native.execute("""
            SELECT submitted.source_tx, submitted.mint, event.id, event.pool, event.quote
            FROM (
                SELECT source_tx, mint FROM bsc_lane_live
                WHERE typeof(our_tx)='text' AND length(our_tx)=66 AND substr(our_tx,1,2)='0x'
                  AND substr(lower(our_tx),3) NOT GLOB '*[^0-9a-f]*' AND lower(our_tx)!=?
                ORDER BY id DESC LIMIT 256
            ) AS submitted
            LEFT JOIN bsc_lane_events AS event ON event.id=(
                SELECT id FROM bsc_lane_events
                WHERE tx_hash=submitted.source_tx AND mint=submitted.mint AND mode='live'
                ORDER BY id DESC LIMIT 1
            )
        """, ("0x" + "0" * 64,))
        for source, mint, event_id, pool, quote in rows:
            source, mint = _fixed_hex(source, 32), _fixed_hex(mint, 20)
            if not source or not mint or int(source, 16) == 0 or int(mint, 16) == 0:
                continue
            if event_id is None:
                return None                    # a submitted row without its event is incomplete
            pool, quote = _fixed_hex(pool, 20), _fixed_hex(quote, 20)
            if (not pool or not quote or int(quote, 16) == 0 or quote in (mint, WBNB)
                    or our_pair(mint, quote) != pool):
                continue
            pins[our_pair(WBNB, quote)] = quote
        return pins
    except (sqlite3.Error, OSError) as exc:
        print(("BSC hop pins unreadable: %s: %s" % (type(exc).__name__, exc))[:300], flush=True)
        return None
    finally:
        if native is not None:
            native.close()
        if ledger is not None:
            ledger.close()


def seed_first(c):
    """F3: the hops a new window seeds first: the configured quotes' pairs and the pins."""
    return set(tape_hop_pairs()) | {r[0] for r in c.execute("SELECT pool FROM hop_pins")}


def apply_route_pins(c, pins, now):
    """True when `pins` replaced the pinned set; None (an unreadable ledger) keeps the old one."""
    if pins is None:
        return False
    remember_hop_pins(c, pins, now)
    return True


def discovered_hop_pairs(c):
    """The pinned pairs plus the HOP_TARGET_CAP newest PairCreated targets."""
    from sniper.services.memecoin_bsc_fast_lane import our_pair
    rows = list(c.execute("SELECT pool,quote_token FROM hop_pins")) + list(c.execute(
        "SELECT pool,quote_token FROM hop_targets ORDER BY observed DESC LIMIT ?", (HOP_TARGET_CAP,)))
    return {r["pool"]: r["quote_token"] for r in rows
            if re.fullmatch(r"0x[0-9a-f]{40}", r["quote_token"])
            and r["quote_token"] != WBNB and r["pool"] == our_pair(WBNB, r["quote_token"])}


def hop_seed_url():
    return os.environ.get(HOP_SEED_URL_VAR, "").strip() or None


_SEED_QUOTA = re.compile(r"-32001|usage limit|quota|current plan", re.I)
_SEED_LIMIT = re.compile(r"\b429\b|'code': 15\b|\"code\": 15\b|-32005|rate limit|too many", re.I)


def seed_failure_kind(exc):
    """"quota" (the plan's allowance is spent: stop for the window), "limit" (a rate limit, possibly
    another reader's on the shared account: back off and retry) or "skip" (transport, timeout, any
    other answer: skip the pair, back off)."""
    status = (exc.code if isinstance(exc, urllib.error.HTTPError)
              else getattr(getattr(exc, "response", None), "status_code", None))
    text = str(exc)
    if _SEED_QUOTA.search(text):
        return "quota"
    if status == 429 or _SEED_LIMIT.search(text):
        return "limit"
    return "skip"


def _redact(text, url):
    """A seed failure line never carries the key: requests puts the whole URL in its errors."""
    from urllib.parse import urlsplit
    text = str(text).replace(str(url), urlsplit(str(url)).hostname or "?")
    return re.sub(r"dkey=[^&\s'\"]+", "dkey=***", text)


def _paced_seed_rpc(method, params, url):
    """One seed read through the dRPC account's shared pacer: a limit answer pauses every reader."""
    from sniper.services.memecoin_research_pacer import drpc_paced

    def call(target, m, p):
        try:
            return _rpc(m, p, target, timeout=HOP_SEED_TIMEOUT_S), None
        except Exception as exc:          # handed back so the pacer sees the limit, then re-raised
            return None, exc
    result, error = drpc_paced(call)(url, method, params)
    if error is not None:
        raise error
    return result


def seed_hop_reserves(hops, height, *, rpc=None, sleep=time.sleep):
    """([(pair, reserve0, reserve1)], stopped) read at block `height` on the seed host, one call per
    pair, at most one a second. A quota answer or no host stops the seed for the window
    (stopped=True); a rate limit keeps what was read and returns for the seeder's backoff; any other
    failure skips the pair. A "0x" answer is no contract at `height` (a pair created after it, or
    never): (pair, None, None), done for the window, since a created pair's own Sync is recorded
    live (F1a: 17,070 of 28,204 failed seeds were such retries). Never raises; a paused account
    returns at once, never blocking."""
    url = hop_seed_url()
    if not url:
        print("BSC hop seed off: %s is not set" % HOP_SEED_URL_VAR, flush=True)
        return [], True
    paused_s = None
    if rpc is None:
        from sniper.services.memecoin_research_pacer import drpc_paused_s as paused_s
        rpc = _paced_seed_rpc
    out = []
    for i, pair in enumerate(sorted(hops)):
        if i:
            sleep(HOP_SEED_GAP_S)
        # Before EVERY pair (Gamma LOW-1): another process can pause the account between pairs,
        # and the pacer would then hold this tape worker up to the pause.
        wait = paused_s(url) if paused_s is not None else 0
        if wait > 0:
            print("BSC hop seed waits after %d of %d: the dRPC account is paused %.0fs"
                  % (len(out), len(hops), wait), flush=True)
            return out, False
        try:
            raw = rpc("eth_call", [{"to": pair, "data": "0x0902f1ac"}, hex(int(height))], url)
            if str(raw).lower() == "0x":
                print(f"BSC hop seed absent {pair} at {int(height)}: no contract", flush=True)
                out.append((pair, None, None))
                continue
            words = str(raw)[2:]
            out.append((pair, int(words[0:64], 16), int(words[64:128], 16)))
        except Exception as exc:
            kind, text = seed_failure_kind(exc), _redact("%s: %s" % (type(exc).__name__, exc), url)
            if kind == "quota":
                print(("BSC hop seed stopped for the window on a quota answer after %d of %d: %s"
                       % (len(out), len(hops), text))[:300], flush=True)
                return out, True
            if kind == "limit":
                print(("BSC hop seed rate-limited after %d of %d; backing off: %s"
                       % (len(out), len(hops), text))[:300], flush=True)
                return out, False
            print(f"BSC hop seed failed {pair}: {text}"[:300], flush=True)
    return out, False


class HopSeeder:
    """Which hop pairs still need a seed in the current coverage window, and when to try next. A
    pair leaves when it is seeded or its own Sync is recorded; failures and rate limits back off
    (HOP_SEED_BACKOFF_S, last step repeats); a quota answer or no seed host ends the window. Within a
    round, `first` (the configured and pinned hops, F3) go before the PairCreated targets."""

    def __init__(self, hops, first=()):
        self.hops, self.first = set(hops), set(first)
        self.priority, self.tried, self.round = [], {}, 0
        self.since, self.start, self.pending = None, None, set()
        self.failures, self.next_at, self.stopped = 0, 0.0, False

    def update_hops(self, hops):
        new = set(hops) - self.hops
        self.hops = set(hops)
        self.pending.intersection_update(self.hops)
        self.tried = {p: n for p, n in self.tried.items() if p in self.hops}
        self.pending.update(new)
        self.priority = [p for p in self.priority if p in self.pending] + sorted(new)
        if new and not self.stopped:
            self.next_at = 0.0

    def batch(self):
        # ponytail: four reads per job; new targets jump ahead of cold/absent pairs.
        chosen = list(dict.fromkeys(list(reversed(self.priority)) + sorted(self.pending)))
        chosen = [p for p in chosen if p in self.pending]
        chosen.sort(key=lambda p: (self.tried.get(p, -1), p not in self.first))
        return set(chosen[:4])

    def window(self, since, start_height):
        """A new coverage window (or the same one again): every pair needs a seed."""
        if since is None or since == self.since:
            return
        self.since, self.start = since, int(start_height)
        self.tried, self.round = {}, 0
        self.pending, self.failures, self.next_at, self.stopped = set(self.hops), 0, 0.0, False

    def saw_sync(self, pool):
        self.pending.discard(pool)

    def due(self, now):
        return bool(self.pending) and not self.stopped and now >= self.next_at

    def height_for(self, head):
        return max(self.start, int(head) - HOP_SEED_LAG_BLOCKS)

    def done(self, seeded, stopped, now, attempted=None):
        self.round += 1
        self.tried.update({p: self.round for p in (attempted or ()) if p in self.hops})
        self.pending -= set(seeded)
        if stopped:
            self.stopped = True
        elif self.pending:
            if attempted is not None and seeded and self.pending - set(attempted):
                self.next_at = now                 # new/unattempted targets are not failed reads
                return
            self.next_at = now + HOP_SEED_BACKOFF_S[min(self.failures, len(HOP_SEED_BACKOFF_S) - 1)]
            self.failures += 1


def record_seed(c, pool, height, block_hash, reserve0, reserve1, received):
    """A seed is a Sync at index -1 of its block: any real Sync in that block or later wins."""
    record_sync(c, pool, height, block_hash, reserve0, reserve1, received, idx=-1)


def _pool_tokens(token0, token1, mint_decimals):
    tokens = (str(token0).lower(), str(token1).lower())
    if WBNB not in tokens or len(set(tokens)) != 2 or type(mint_decimals) is not int \
            or not 0 <= mint_decimals <= 255:
        return None
    return tokens


def record_pool(c, pool, token0, token1, mint_decimals):
    tokens = _pool_tokens(token0, token1, mint_decimals)
    if tokens is None:
        return False
    c.execute("INSERT OR REPLACE INTO pools VALUES (?,?,?,?)",
              (str(pool).lower(), *tokens, mint_decimals))
    c.commit()
    return True


def record_v3_pool(c, pool, token0, token1, mint_decimals):
    tokens = _pool_tokens(token0, token1, mint_decimals)
    if tokens is None:
        return False
    c.execute("INSERT OR REPLACE INTO v3_pools VALUES (?,?,?,?)",
              (str(pool).lower(), *tokens, mint_decimals))
    c.commit()
    return True


def record_v3_swap(c, pool, height, block_hash, sqrt_price_x96, received, idx=0):
    c.execute("INSERT INTO v3_marks VALUES (?,?,?,?,?,?) ON CONFLICT(pool) DO UPDATE SET "
              "height=excluded.height,hash=excluded.hash,idx=excluded.idx,"
              "sqrt_price_x96=excluded.sqrt_price_x96,received=excluded.received "
              "WHERE excluded.height>v3_marks.height OR "
              "(excluded.height=v3_marks.height AND excluded.idx>=v3_marks.idx)",
              (str(pool).lower(), int(height), str(block_hash).lower(), int(idx),
               str(sqrt_price_x96), float(received)))
    c.commit()


def record_quote(c, usd, observed):
    try:
        usd = float(usd)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(usd) or usd <= 0:
        return False
    c.execute("INSERT OR REPLACE INTO quote VALUES (1,?,?)", (usd, float(observed)))
    c.commit()
    return True


def _fresh_quote(c, now):
    row = c.execute("SELECT usd,observed FROM quote WHERE id=1").fetchone()
    return (float(row["usd"]) if row and row["usd"] and
            0 <= now - row["observed"] <= QUOTE_TTL_S else None)


def latest_mark(c, mint, pool, now):
    """Verified reserve mid; never substitute it for an executable whole-bag quote."""
    now, mint, pool = float(now), str(mint).lower(), str(pool).lower()
    row = c.execute("SELECT s.*,p.token0,p.token1,p.mint_decimals,h.block_ts "
                    "FROM syncs s JOIN pools p ON p.pool=s.pool "
                    "JOIN heads h ON h.height=s.height AND h.hash=s.hash WHERE s.pool=?", (pool,)).fetchone()
    if not row or {row["token0"], row["token1"]} != {mint, WBNB} \
            or not 0 <= now - row["received"] <= PRICE_TTL_S \
            or not 0 <= row["received"] - row["block_ts"] <= DELIVERY_MAX_LAG_S:
        return None
    feed = c.execute("SELECT received FROM feed WHERE id=1").fetchone()
    if not feed or feed["received"] is None or not 0 <= now - feed["received"] <= HEAD_TTL_S:
        return None
    latest_swap = c.execute("SELECT s.tx,s.height,s.idx FROM swaps s JOIN heads h "
                            "ON h.height=s.height AND h.hash=s.hash WHERE s.pool=? "
                            "ORDER BY s.height DESC,s.idx DESC LIMIT 1", (pool,)).fetchone()
    sync_source = c.execute("SELECT tx,height,idx FROM sync_sources WHERE pool=?", (pool,)).fetchone()
    if latest_swap and (latest_swap["height"] > row["height"] or (
            latest_swap["height"] == row["height"] and latest_swap["idx"] > row["idx"]
            and (not sync_source or latest_swap["tx"] != sync_source["tx"]))):
        return None
    usd = _fresh_quote(c, now)
    if usd is None:
        return None
    quote_side = 0 if row["token0"] == WBNB else 1
    try:
        quote = int(row["reserve0"] if quote_side == 0 else row["reserve1"])
        token = int(row["reserve1"] if quote_side == 0 else row["reserve0"])
        price = (quote / 10**18) / (token / 10**int(row["mint_decimals"])) * usd
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return {"price": price, "observed_at": row["received"], "block_ts": row["block_ts"],
            "source": "bsc_v2_sync"}


def latest_v3_mark(c, mint, pool, now):
    now, mint, pool = float(now), str(mint).lower(), str(pool).lower()
    row = c.execute("SELECT s.*,p.token0,p.token1,p.mint_decimals,h.block_ts "
                    "FROM v3_marks s JOIN v3_pools p ON p.pool=s.pool "
                    "JOIN heads h ON h.height=s.height AND h.hash=s.hash WHERE s.pool=?", (pool,)).fetchone()
    if not row or {row["token0"], row["token1"]} != {mint, WBNB} \
            or not 0 <= now - row["received"] <= PRICE_TTL_S \
            or not 0 <= row["received"] - row["block_ts"] <= DELIVERY_MAX_LAG_S:
        return None
    feed = c.execute("SELECT received FROM feed WHERE id=1").fetchone()
    if not feed or feed["received"] is None or not 0 <= now - feed["received"] <= HEAD_TTL_S:
        return None
    usd = _fresh_quote(c, now)
    if usd is None:
        return None
    try:
        ratio = (int(row["sqrt_price_x96"]) / 2**96) ** 2
        price = (ratio if row["token0"] == mint else 1 / ratio) \
            * 10 ** (int(row["mint_decimals"]) - 18) * usd
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return {"price": price, "observed_at": row["received"], "block_ts": row["block_ts"],
            "source": "bsc_v3_swap"}


# $1 BSC stables (18 decimals on BSC). A pair's quote is one of these only when the pair's address is
# its PancakeSwap-V2 CREATE2 address against it (`quote_of_pair`): never assumed from a name.
STABLES = ("0x55d398326f99059ff775485246999027b3197955",      # USDT (BSC-USD)
           "0xe9e7cea3dedca5984780bafc599bd69add087d56",      # BUSD
           "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d")      # USDC (Binance-Peg)


def _pcs_v2_pair(mint, quote):
    from sniper.services.memecoin_evm_post_trade import V2_FACTORIES, _v2_pair
    factory, init = V2_FACTORIES[(CHAIN, "0x10ed43c718714eb63d5aa57b78b54704e256024e")]
    return _v2_pair(factory, init, str(mint).lower(), str(quote).lower()).lower()


# PancakeSwap V3 CREATE2: the pool deployer and init-code hash, proved on 7 of 7 stored v3_pools
# (2026-10-08), at the four fee tiers.
PCS_V3_DEPLOYER = "0x41ff9aa7e16b8b1a8a8dc4f0efacd93d02d071c9"
PCS_V3_INIT = "0x6ce8eb472fa82df5469c6ab6d485f17c3ad13c8cd7af59b3d4a8026c5ce0f7e2"
PCS_V3_FEES = (100, 500, 2500, 10000)


def _pcs_v3_pool(mint, quote, fee):
    from eth_abi import encode
    from eth_utils import keccak
    a, b = sorted((str(mint).lower(), str(quote).lower()), key=lambda t: int(t, 16))
    salt = keccak(encode(["address", "address", "uint24"], [a, b, int(fee)]))
    raw = keccak(b"\xff" + bytes.fromhex(PCS_V3_DEPLOYER[2:]) + salt + bytes.fromhex(PCS_V3_INIT[2:]))
    return "0x" + raw[12:].hex()


def quote_of_pair(mint, pool):
    """WBNB or a $1 stable when `pool` is the mint's PancakeSwap-V2 pair or PancakeSwap-V3 pool (any
    fee tier) against it, else None (a launchpad base or another factory: countable, not priceable
    here)."""
    pool = str(pool or "").lower()
    for quote in (WBNB,) + STABLES:
        try:
            if _pcs_v2_pair(mint, quote) == pool or any(
                    _pcs_v3_pool(mint, quote, fee) == pool for fee in PCS_V3_FEES):
                return quote
        except (KeyError, ValueError, TypeError):
            return None
    return None


def source_pair(mint):
    """The PancakeSwap-V2 mint/WBNB pair (CREATE2 from the factory and init-code hash PROVEN against
    our own receipts in memecoin_evm_post_trade.V2_FACTORIES), lowercase, or None. The m5 read takes
    it only when the source transaction swapped in it, so this names the pool of the source's own
    mint-moving swap without the provider's pair pick (entry-gates spec step 4)."""
    try:
        return _pcs_v2_pair(mint, WBNB)
    except (KeyError, ValueError, TypeError):
        return None


def m5_bound(c, pool, quote_token, mint, source_tx, now, *, coin_is_token1=None):
    """One source-venue lower bound, valid only after a continuous five-minute window.

    `coin_is_token1` (the source receipt's decode, which names the pool that moved the mint): the
    quote side comes from it and `quote_token` may be None or any base. USD is priced only for WBNB
    or a proved $1 stable (`quote_of_pair`); any other base returns the window as `counts_only`, with
    buys and sells but no USD, which E7 reads and the volume gate does not."""
    unknown = {"usd": None, "reason": "uncovered_bsc_v2", "coverage": "unknown"}
    pool, mint = str(pool).lower(), str(mint).lower()
    quote_token = str(quote_token).lower() if quote_token else None
    source_tx, now = str(source_tx).lower(), float(now)
    if len(pool) != 42 or len(mint) != 42 or not source_tx.startswith("0x") or len(source_tx) != 66:
        return unknown
    if coin_is_token1 is None and quote_token != WBNB:
        return unknown
    if coin_is_token1 is not None and quote_token is None:
        quote_token = quote_of_pair(mint, pool)
    feed = c.execute("SELECT since,head_ts,received FROM feed WHERE id=1").fetchone()
    if not feed or feed["since"] is None or feed["since"] > now - 300 \
            or feed["received"] is None or not 0 <= now - feed["received"] <= 3:
        return unknown
    if quote_token == WBNB:
        usd = _fresh_quote(c, now)
        if usd is None:
            return {**unknown, "reason": "quote_stale"}
    else:
        usd = 1.0 if quote_token in STABLES else None
    if coin_is_token1 is not None:
        quote_side = 0 if coin_is_token1 else 1
    else:
        quote_side = 0 if quote_token < mint else 1
    rows = c.execute("SELECT s.tx,s.amount0,s.amount1 FROM swaps s "
                     "JOIN heads h ON h.height=s.height AND h.hash=s.hash "
                     "WHERE s.pool=? AND h.block_ts>? AND h.block_ts<=?",
                     (pool, now - 300, now)).fetchall()
    if not any(str(r["tx"] or "").strip().lower() == source_tx for r in rows):
        return {**unknown, "reason": "source_tx_absent"}
    other_rows = [r for r in rows if str(r["tx"] or "").strip().lower() != source_tx]
    txs = {str(r["tx"] or "").strip().lower() for r in other_rows}
    txns = None if "" in txs else len(txs)
    others = [int(r["amount0"] if quote_side == 0 else r["amount1"]) for r in other_rows]
    total = sum(abs(q) for q in others)
    # House rule: positive flows INTO the pool, so quote in (> 0) is a buy of the coin.
    buys, sells = sum(q > 0 for q in others), sum(q < 0 for q in others)
    usd_total = None if usd is None else total / 10**18 * usd
    return {"usd": usd_total, "reason": "ok" if usd_total is not None else "counts_only",
            "coverage": "source_pool_lower_bound",
            "swaps": len(others), "buys": buys, "sells": sells, "txns": txns,
            "pools": [{"pool": pool, "buys": buys, "sells": sells, "swaps": len(others),
                       "usd": usd_total, "txns": txns}]}


def read_m5(pool, quote_token, mint, source_tx, *, coin_is_token1=None):
    try:
        c = connect(readonly=True)
        try:
            return m5_bound(c, pool, quote_token, mint, source_tx, real_utc_now(),
                            coin_is_token1=coin_is_token1)
        finally:
            c.close()
    except (OSError, sqlite3.Error):
        return {"usd": None, "reason": "bsc_store_unavailable", "coverage": "unknown"}


def read_marks(positions, now):
    if not any(str(p.get("network_id")) == CHAIN for p in positions):
        return {}
    try:
        c = connect(readonly=True)
        try:
            out = {}
            for p in positions:
                if str(p.get("network_id")) != CHAIN:
                    continue
                pool = _position_pool(p)
                if not pool:
                    continue
                mark = latest_mark(c, p["mint"], pool, now) or latest_v3_mark(c, p["mint"], pool, now)
                if mark:
                    out[int(p["id"])] = mark
            return out
        finally:
            c.close()
    except (OSError, sqlite3.Error):
        return {}


def reset_line(exc, now=None):
    """The observer's reset line, stamped so the reset rate can be read from the log."""
    at = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).isoformat()
    return "%s BSC observer reset: %s: %s" % (at, type(exc).__name__, exc)


def _rpc(method, params, url=RPC, timeout=3):
    import requests
    response = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                        "method": method, "params": params}, timeout=timeout)
    response.raise_for_status()
    answer = response.json()
    if answer.get("error") or "result" not in answer:
        raise RuntimeError("BSC observation RPC failed: " + str(answer.get("error")))
    return answer["result"]


def _resolve(pool, mint, url):
    def call(to, data):
        return _rpc("eth_call", [{"to": to, "data": data}, "latest"], url)
    t0 = "0x" + call(pool, "0x0dfe1681")[-40:].lower()
    t1 = "0x" + call(pool, "0xd21220a7")[-40:].lower()
    if {t0, t1} != {mint, WBNB}:
        return None
    decimals = int(call(mint, "0x313ce567"), 16)
    return t0, t1, decimals


def _resolve_v3(pool, mint, fee, url):
    pair = _resolve(pool, mint, url)
    if pair is None:
        return None
    from sniper.settings.memecoin import MEMECOIN_EVM_V3_QUOTER
    pool_fee = int(_rpc("eth_call", [{"to": pool, "data": "0xddca3f43"}, "latest"], url), 16)
    factory = _rpc("eth_call", [{"to": pool, "data": "0xc45a0155"}, "latest"], url)[-40:].lower()
    expected = _rpc("eth_call", [{"to": MEMECOIN_EVM_V3_QUOTER[CHAIN],
                                    "data": "0xc45a0155"}, "latest"], url)[-40:].lower()
    return pair if (fee is None or pool_fee == fee) and factory == expected else None


def _position_pool(p):
    pool = p.get("executed_pool")
    if pool:
        return str(pool).lower()
    try:
        contract = json.loads(p.get("funded_contract_json") or "{}")
        route = json.loads(p.get("evm_route_json") or "{}")
    except (TypeError, ValueError):
        return None
    if route.get("protocol") not in ("pancake_v3", "uniswap_v2"):
        return None
    if route.get("protocol") == "pancake_v3" and (
            not route.get("tokens") or not route.get("fees")):
        return None
    pool = ((contract.get("classification") or {}).get("facts") or {}).get("liquidity_pair_address")
    return str(pool).lower() if pool and str(pool).startswith("0x") and len(str(pool)) == 42 else None


def _open_pools():
    from sniper.persistence.db_core import get_readonly_connection
    c = get_readonly_connection()
    try:
        c.execute("PRAGMA busy_timeout=100")
        v2, v3 = {}, {}
        for row in c.execute("SELECT executed_pool,mint,evm_route_json,funded_contract_json "
                             "FROM memecoin_positions WHERE network_id='56' "
                             "AND execution_mode='real' AND is_open=1 AND tracking_paused_at IS NULL"):
            p = dict(row)
            pool = _position_pool(p)
            if not pool:
                continue
            try:
                route = json.loads(p.get("evm_route_json") or "{}")
                protocol = route.get("protocol")
            except (TypeError, ValueError, AttributeError):
                continue
            if protocol == "pancake_v3" and route.get("fees"):
                try:
                    fee = int(route["fees"][0]) if len(route.get("tokens") or []) == 2 else None
                    v3[pool] = (str(p["mint"]).lower(), fee)
                except (TypeError, ValueError):
                    continue
            elif protocol == "uniswap_v2":
                v2[pool] = str(p["mint"]).lower()
        return v2, v3
    finally:
        c.close()


def run(*, ws_url=WS, rpc_url=RPC, candidate_counts=None):
    """Read-only chain observer; its SQLite file is separate from the funded ledger."""
    from websockets.sync.client import connect as ws_connect
    if not ws_url.startswith("wss://") or not rpc_url.startswith("https://"):
        raise ValueError("BSC observation requires secure public endpoints")
    if int(_rpc("eth_chainId", [], rpc_url), 16) != 56:
        raise ValueError("BSC observation RPC is on the wrong chain")
    c = connect()
    workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bsc-mark")
    from sniper.services.memecoin_bsc_execution_observation import start_tape_observer
    execution_observer = start_tape_observer(rpc_url=rpc_url)
    hops = tape_hop_pairs()
    discovery_on = bool(hops)
    from sniper.services.memecoin_bsc_fast_lane import presignal_warm_enabled
    warm_enabled = presignal_warm_enabled()
    candidate_counts = {} if candidate_counts is None else candidate_counts
    candidate_counts.update(discovery_active=int(discovery_on), discovery_inactive=int(not discovery_on),
                            presignal_warm_enabled=int(warm_enabled), published=0)
    print("BSC native candidate discovery active=%d inactive=%d presignal_warm_enabled=%d" % (
        candidate_counts["discovery_active"], candidate_counts["discovery_inactive"],
        candidate_counts["presignal_warm_enabled"]), flush=True)
    if discovery_on:
        apply_route_pins(c, read_route_pins(), real_utc_now())
        hops.update(discovered_hop_pairs(c))
    pins_job, next_pins = None, real_utc_now() + HOP_PIN_REFRESH_S
    heads_skipped = 0
    try:
        delay = 1
        while True:
            reset_coverage(c)
            try:
                with ws_connect(ws_url, open_timeout=10, max_size=None) as ws:
                    ws.send(json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}))
                    answer = json.loads(ws.recv(timeout=10))
                    if int(answer.get("result", "0x0"), 16) != 56:
                        raise ValueError("BSC observer connected to wrong chain")
                    for rid, params in ((2, ["newHeads"]),
                                        (3, ["logs", {"topics": [V2_SWAP]}]),
                                        (4, ["logs", {"topics": [V2_SYNC]}]),
                                        # PancakeSwap V3 has its own Swap topic (two extra
                                        # fee words): both, so a PCS V3 source is counted.
                                        (5, ["logs", {"topics": [[V3_SWAP, PANCAKE_V3_SWAP]]}])):
                        ws.send(json.dumps({"jsonrpc":"2.0","id":rid,
                                            "method":"eth_subscribe","params":params}))
                    if discovery_on:
                        ws.send(json.dumps({"jsonrpc":"2.0","id":6,"method":"eth_subscribe",
                                            "params":["logs", {"address": PCS_V2_FACTORY,
                                                                "topics": [PAIR_CREATED]}]}))
                    subs, held, held_v3, pending = {}, {}, {}, {}
                    v3_requests, v3_retry_at, resolve_retry_at = {}, {}, {}
                    next_sub_id = 100
                    connected_at = last_head = last_pair_created = real_utc_now()
                    pair_rejected = False
                    next_positions = next_quote = 0.0
                    next_ping = real_utc_now() + 20
                    quote_job = None
                    seed_job, seed_height, seeder = None, None, HopSeeder(hops, first=seed_first(c))
                    while True:
                        now = real_utc_now()
                        if now >= next_ping:
                            ws.ping()
                            next_ping = now + 20
                        required = {2, 3, 4, 5}
                        if now - connected_at > 10 and not required.issubset(subs.values()):
                            raise TimeoutError("BSC subscriptions did not acknowledge")
                        if {2, 3, 4}.issubset(subs.values()) and now - last_head > 5:
                            raise TimeoutError("BSC head stream stalled")
                        pair_bound = PAIR_CREATED_REJECTED_RETRY_S if pair_rejected else PAIR_CREATED_SILENT_S
                        if discovery_on and now - last_pair_created > pair_bound:
                            # Counted from connect, so a PairCreated subscription that never acks or
                            # never delivers is the same silence; a refused one says so.
                            raise TimeoutError("BSC PairCreated stream %s %.0fs" % (
                                "rejected" if pair_rejected else "silent", now - last_pair_created))
                        if now >= next_positions:
                            next_positions = now + 1
                            try:
                                held, held_v3 = _open_pools()
                            except sqlite3.Error as exc:
                                print(f"BSC held-pool read failed: {exc}"[:300], flush=True)
                            for pool, mint in held.items():
                                if now >= resolve_retry_at.get(pool, 0) and pool not in pending and not c.execute(
                                        "SELECT 1 FROM pools WHERE pool=? AND (token0=? OR token1=?)",
                                        (pool, mint, mint)).fetchone():
                                    pending[pool] = workers.submit(_resolve, pool, mint, rpc_url)
                            for pool, (mint, fee) in held_v3.items():
                                if now >= resolve_retry_at.get(pool, 0) and pool not in pending and not c.execute(
                                        "SELECT 1 FROM v3_pools WHERE pool=? AND (token0=? OR token1=?)",
                                        (pool, mint, mint)).fetchone():
                                    pending[pool] = workers.submit(_resolve_v3, pool, mint, fee, rpc_url)
                                if now >= v3_retry_at.get(pool, 0) and not any(
                                        request_pool == pool for request_pool, _ in v3_requests.values()) and not any(
                                        kind == ("v3", pool) for kind in subs.values()):
                                    next_sub_id += 1
                                    v3_requests[next_sub_id] = (pool, now)
                                    ws.send(json.dumps({"jsonrpc":"2.0","id":next_sub_id,
                                        "method":"eth_subscribe","params":["logs",
                                        {"address":pool,"topics":[[V3_SWAP, PANCAKE_V3_SWAP]]}]}))
                            for sid, kind in list(subs.items()):
                                if isinstance(kind, tuple) and kind[0] == "v3" and kind[1] not in held_v3:
                                    next_sub_id += 1
                                    ws.send(json.dumps({"jsonrpc":"2.0","id":next_sub_id,
                                                        "method":"eth_unsubscribe","params":[sid]}))
                                    del subs[sid]
                        for pool, job in list(pending.items()):
                            if job.done():
                                try:
                                    result = job.result()
                                    if result:
                                        if pool in held_v3:
                                            record_v3_pool(c, pool, *result)
                                        else:
                                            record_pool(c, pool, *result)
                                    else:
                                        resolve_retry_at[pool] = now + 30
                                except Exception as exc:
                                    resolve_retry_at[pool] = now + 30
                                    print(f"BSC pool resolve failed {pool}: {type(exc).__name__}: {exc}"[:300], flush=True)
                                del pending[pool]
                        for rid, (pool, sent_at) in list(v3_requests.items()):
                            if now - sent_at > 10:
                                v3_retry_at[pool] = now + 30
                                del v3_requests[rid]
                                print(f"BSC V3 subscription timed out {pool}"[:300], flush=True)
                        if now >= next_quote and quote_job is None:
                            from sniper.execution.evm_wallet import quote_usd_price
                            quote_job = workers.submit(quote_usd_price, CHAIN)
                            next_quote = now + 30
                        if seed_job is not None and seed_job.done():
                            seeded, stopped = (), False
                            try:
                                head = c.execute("SELECT hash FROM heads WHERE height=?",
                                                 (seed_height,)).fetchone()
                                got, stopped = seed_job.result()
                                for pair, r0, r1 in (got if head else ()):
                                    if r0 is not None:          # None: no contract at that block
                                        record_seed(c, pair, seed_height, head[0], r0, r1, real_utc_now())
                                seeded = [pair for pair, _r0, _r1 in got] if head else ()
                            except Exception as exc:
                                print(f"BSC hop seed record failed: {exc}"[:300], flush=True)
                            seeder.done(seeded, stopped, real_utc_now(), attempted=seed_batch)
                            seed_job = None
                        if discovery_on and pins_job is None and now >= next_pins:
                            pins_job = workers.submit(read_route_pins)
                            next_pins = now + HOP_PIN_REFRESH_S
                        if pins_job is not None and pins_job.done():
                            try:
                                pins = pins_job.result()
                            except Exception as exc:       # the worker's own bug: keep the old pins
                                pins = None
                                print(f"BSC hop pins read failed: {type(exc).__name__}: {exc}"[:300], flush=True)
                            pins_job = None
                            if apply_route_pins(c, pins, real_utc_now()):
                                hops = {**tape_hop_pairs(), **discovered_hop_pairs(c)}
                                seeder.first = seed_first(c)
                                seeder.update_hops(hops)
                        if quote_job and quote_job.done():
                            try:
                                record_quote(c, quote_job.result(), real_utc_now())
                            except Exception:
                                pass
                            quote_job = None
                        try:
                            msg = json.loads(ws.recv(timeout=0.25))
                        except TimeoutError:
                            continue
                        if msg.get("error") and msg.get("id") == 6:
                            print("BSC pair discovery rejected; core tape remains active", flush=True)
                            pair_rejected = True
                            continue
                        if msg.get("error") and msg.get("id") in v3_requests:
                            pool, _ = v3_requests.pop(msg["id"])
                            v3_retry_at[pool] = now + 30
                            print(f"BSC V3 subscription rejected {pool}: {msg['error']}"[:300], flush=True)
                            continue
                        if msg.get("error"):
                            raise RuntimeError("BSC subscription rejected")
                        if msg.get("id") in (2, 3, 4, 5, 6) and msg.get("result"):
                            subs[msg["result"]] = msg["id"]
                            continue
                        if msg.get("id") in v3_requests and msg.get("result"):
                            pool, _ = v3_requests.pop(msg["id"])
                            subs[msg["result"]] = ("v3", pool)
                            continue
                        if not {2, 3, 4, 5}.issubset(subs.values()):
                            continue
                        item = (msg.get("params") or {}).get("result") or {}
                        kind = subs.get((msg.get("params") or {}).get("subscription"))
                        now = real_utc_now()
                        if kind == 2:
                            if not item.get("number") or not item.get("hash"):
                                # A head with no number or hash: skipped and counted, never a reset
                                # (a reset wipes the Syncs and needs a re-seed; Alpha 2026-10-02).
                                heads_skipped += 1
                                if heads_skipped == 1 or heads_skipped % 100 == 0:
                                    print("%s BSC head without number/hash skipped (n=%d)" % (
                                        datetime.now(timezone.utc).isoformat(), heads_skipped),
                                        flush=True)
                                continue
                            record_head(c, int(item["number"], 16), item["hash"],
                                        int(item["timestamp"], 16), now,
                                        parent_hash=item.get("parentHash"))
                            if execution_observer is not None:
                                execution_observer.publish_head(item)
                            last_head = now
                            delay = 1
                            since = c.execute("SELECT since, height FROM feed WHERE id=1").fetchone()
                            seeder.window(since[0], since[1])
                            if seed_job is None and seeder.due(now):
                                seed_height = seeder.height_for(int(item["number"], 16))
                                seed_batch = seeder.batch()
                                seed_job = workers.submit(seed_hop_reserves, seed_batch,
                                                          seed_height)
                        elif kind == 6:
                            last_pair_created = now        # any delivery, kept or not, is liveness
                            targets = created_hop_targets(item)
                            if targets:
                                if warm_enabled:
                                    candidate_counts["published"] += publish_native_candidates(item, time.time())
                                remember_hop_targets(c, targets, now)
                                hops = {**tape_hop_pairs(), **discovered_hop_pairs(c)}
                                seeder.update_hops(hops)
                        elif kind in (3, 4, 5) or isinstance(kind, tuple):
                            if item.get("removed"):
                                reset_coverage(c)
                                continue
                            row = decode_evm_log(CHAIN, item, datetime.fromtimestamp(now, timezone.utc).isoformat())
                            if not row or not row.get("block_hash"):
                                continue
                            if kind == 3 and row["kind"] == "v2_swap":
                                record_swap(c, row["pool"], row["block_no"], row["block_hash"],
                                            row["tx_sig"], row["log_index"], row["amount0"], row["amount1"])
                            elif kind == 5 and row["kind"] == "v3_swap":
                                record_swap(c, row["pool"], row["block_no"], row["block_hash"],
                                            row["tx_sig"], row["log_index"], row["amount0"], row["amount1"])
                            elif kind == 4 and row["kind"] == "v2_sync" and records_sync(
                                    row["pool"], held=held, hops=hops):
                                record_sync(c, row["pool"], row["block_no"], row["block_hash"],
                                            row["amount0"], row["amount1"], now, row["log_index"],
                                            tx=row["tx_sig"])
                                seeder.saw_sync(row["pool"])
                            elif isinstance(kind, tuple) and kind[0] == "v3" \
                                    and row["kind"] == "v3_swap" and row["pool"] in held_v3 \
                                    and row.get("sqrt_price_x96"):
                                record_v3_swap(c, row["pool"], row["block_no"], row["block_hash"],
                                               row["sqrt_price_x96"], now, row["log_index"])
            except Exception as exc:
                print(reset_line(exc)[:300], flush=True)
                time.sleep(delay)
                delay = min(30, delay * 2)
    finally:
        if execution_observer is not None:
            execution_observer.close()
        workers.shutdown(wait=False, cancel_futures=True)
        c.close()


if __name__ == "__main__":
    from sniper.util.envfiles import load_env_files
    load_env_files(str(state_root()))
    run(ws_url=os.getenv("MEMECOIN_BSC_LIVE_WS_URL", WS),
        rpc_url=os.getenv("MEMECOIN_BSC_LIVE_RPC_URL", RPC))

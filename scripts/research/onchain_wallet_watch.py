#!/usr/bin/env python3
"""Watch the armed wallets' Solana addresses on-chain and detect their buys.

This is the fomo-bypass. fomo indexes these wallets' swaps and serves them on a REST feed that
its own UI and our runner both poll at ~3s. Subscribing to the wallets DIRECTLY on-chain
(Helius logsSubscribe at `confirmed` commitment, see the subscribe call below) sees the buy at
confirmation. `confirmed` is deliberate: a paired one-hour test on 2026-09-23 (n=74,
docs/audits/2026-09-23-ws-commitment-processed-vs-confirmed.md) found `processed` frames arrive
162 ms earlier but the actionable row lands 214 ms LATER, because getTransaction refuses any
commitment below `confirmed` and the early frame buys a null-retry on 62% of calls. Measured
against the J7 relay the lane is ~0.5 s behind on every head-to-head; that gap is upstream of
this process, not the commitment level.

This process detects and records buys. The active funded runner can consume these records.
For every watched-wallet buy it
records the mint, the SOL spent, and the detection time, then compares against our own
memecoin_events to (a) verify the detection is a real buy we recognize and (b) measure how much
earlier on-chain detection is than the fomo feed caught the same buy. It also auto-confirms the
'strong' (holder-intersection) addresses the first time each wallet's on-chain buyer matches,
and resolves any still-unresolved wallet (MasterChief117) on its next fresh buy.

Wiring detections to the funded entry (make_real_buy_fn's buy_fn) is stage 2 and runs inside the
funded runner with its built contexts — never from this standalone process.

Read-only against the chain and our DB except its own `onchain_wallet_detections` table.
"""
import argparse
import asyncio
import json
import math
import os
import queue
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websockets

REPO = Path(__file__).resolve().parents[2]
# STATE IS NOT CODE (2026-09-28, release pinning). The code comes from wherever this file runs --
# the pinned release under .runtime/memecoin/releases/<sha> once memecoin_watchdog.sh owns it --
# but the ledger, the .env, the heartbeat and the stage log live in the state root, exactly as the
# EVM watcher resolves them. Run from the checkout with no env, both are the repo, as before.
STATE_ROOT = Path(os.getenv("SNIPER_STATE_ROOT") or REPO)
sys.path.insert(0, str(REPO))
if __name__ == "__main__":
    # One write() per print() (sniper/util/line_print.py): before any other sniper import.
    from sniper.util.line_print import install
    install()
from sniper.persistence.db_core import get_readonly_connection
from sniper.persistence.db_memecoin import ensure_onchain_sells_table, record_onchain_sell
from sniper.services.memecoin_onchain_wake import default_address, send_wake
from sniper.services.memecoin_source_valuation import value_source_buy
from sniper.util.clock import real_utc_now
DB = Path(os.getenv("DB_PATH") or STATE_ROOT / "sniper_tracker.db")
# The runner's on-chain lane listens here (2026-09-27): one datagram after each committed detection
# makes it sweep at once instead of at its next 1 s poll. Named from the state root, as the lane is.
WAKE_ADDRESS = default_address()
_DETECTION_INSERT = (
    "INSERT OR IGNORE INTO onchain_wallet_detections (signature, source_user_id, handle, "
    "address, mint, token_delta, sol_spent, block_time, detected_at, detect_latency_s, "
    "feed_event_id, feed_lag_vs_onchain_s, created_at, source_size_usd, source_price, source_valuation_json, "
    "source_slot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def ensure_detection_columns(wdb):
    """The columns this watcher writes that an older table lacks, added at startup (idempotent), so a
    watcher on new code never drops a detection on a table the runner has not migrated yet."""
    have = {r[1] for r in wdb.execute("PRAGMA table_info(onchain_wallet_detections)")}
    for col, ddl in (("source_valuation_json", "TEXT"), ("source_slot", "INTEGER")):
        if col not in have:
            wdb.execute("ALTER TABLE onchain_wallet_detections ADD COLUMN %s %s" % (col, ddl))
    wdb.commit()


def _store_detection(wdb, params):
    """Insert and commit one detection, THEN wake the runner's on-chain lane. The wake follows the
    commit so the sweep it triggers can read the row; it is a hint (the lane's poll covers a lost
    one), so a failed wake never reaches this loop. A failed insert or commit raises, unwoken."""
    stored = wdb.execute(_DETECTION_INSERT, params).rowcount > 0
    wdb.commit()
    try:
        if stored:
            send_wake(WAKE_ADDRESS)
    except Exception:  # noqa: BLE001 - send_wake never raises; this guards a replaced sender
        pass
    return stored
HEARTBEAT = STATE_ROOT / "data" / "onchain_watch.heartbeat"
# THE RELEASE HANDOVER (operator order 2026-10-07). memecoin_watchdog.sh keeps an older-release
# watcher running beside this one until this process has acked every owner it watches, then stops it.
# One line, "<pid> <utc>", written once per process when that first happens; no line = not ready.
# PARTIAL (Gamma LOW, 2026-10-07): "<pid> partial <acked>/<desired>" on the first ack when not every
# owner is acked yet. Not ready; but at the watchdog's cap it shows this process holds a socket, so an
# owner refused on every try ends the handover on the old side instead of relaunching forever.
READY = STATE_ROOT / "data" / "onchain_watch.ready"


def _say_ready(partial=None, *, primary=False):
    """Tell the watchdog this process watches every owner (or, with `partial`, some). False when the
    write failed (the watchdog then ends the handover at its cap instead)."""
    try:
        state = "primary " + now_iso() if primary else now_iso()
        READY.write_text("%d %s\n" % (os.getpid(), state if partial is None else "partial " + partial))
        return True
    except OSError as exc:
        print(f"  ready write failed: {exc}", flush=True)
        return False
# STAGE STAMPS (2026-09-23). Raw Helius frames arrive 0.45 s BEFORE J7, yet this watcher's row
# lands 1.61 s after block time against J7's 1.25 s, so about 0.7 s is inside our process. Which
# stage owns it is NOT obvious: the getTransaction path below can spend two 0.4 s sleeps waiting
# for a just-confirmed transaction to become readable, so a slow "RPC stage" may be mostly WAITING
# rather than round trip. Attempts and sleep are therefore recorded separately from latency, and
# 429s get their own column because a throttled hour measures the throttle, not the watcher.
# Append-only JSONL, never the funded ledger: that lock belongs to the runner.
STAGE_STAMPS = os.getenv("ONCHAIN_WATCH_STAGE_STAMPS", "1").strip().lower() not in {"0", "false", "no"}
STAGE_LOG = STATE_ROOT / "data" / "onchain_watch_stages.jsonl"
RPC_COMMITMENT = os.getenv("ONCHAIN_WATCH_RPC_COMMITMENT", "confirmed").strip() or "confirmed"


def _stage_write(row):
    """One detection's stage timings. Never raises into the detection path."""
    if not STAGE_STAMPS:
        return
    try:
        with open(STAGE_LOG, "a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        pass

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
NATIVE = "11111111111111111111111111111111"
QUOTE_MINTS = {USDC, WSOL, NATIVE}

SCHEMA = """
CREATE TABLE IF NOT EXISTS onchain_wallet_detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT UNIQUE NOT NULL,
    source_user_id TEXT NOT NULL,
    handle TEXT,
    address TEXT NOT NULL,
    network_id TEXT NOT NULL DEFAULT 'solana',
    mint TEXT NOT NULL,
    token_delta REAL,
    sol_spent REAL,
    source_size_usd REAL,
    source_price REAL,
    source_valuation_json TEXT,
    block_time INTEGER,
    detected_at TEXT NOT NULL,
    detect_latency_s REAL,          -- detected_at - block_time
    feed_event_id TEXT,             -- matching memecoin_events row, if any
    feed_lag_vs_onchain_s REAL,     -- how much LATER the fomo feed caught it (+ = we won)
    created_at TEXT NOT NULL,
    source_slot INTEGER,            -- the source buy's slot (fill timing, 2026-10-02)
    -- ONE COLUMN SET WITH db_memecoin.ensure_memecoin_tables (2026-09-29): the on-chain lane's
    -- hot-path SELECT reads source_observation_state, which a table created here lacked until
    -- the runner's ensure ALTERed it in (29 lane tests red). tests/test_onchain_detection_schema_parity.py
    token_amount_atomic TEXT,
    quote_amount_atomic TEXT,
    source_observation_key TEXT,
    source_observation_state TEXT
);
"""


class _FeedMatcher:
    """Optional enrichment on one observation-only handle; a read failure cannot retry a write."""
    def __init__(self, path, stats=None):
        self.path, self.stats, self.conn = path, stats, None

    def _unknown(self, stage, exc):
        locked = (isinstance(exc, sqlite3.OperationalError)
                  and ("locked" in str(exc).lower() or "busy" in str(exc).lower()))
        code = "feed_match_locked" if locked else "feed_match_unavailable"
        stage.update(feed_match_state="unknown", feed_match_error=code)
        if self.stats is not None:
            self.stats['feed_match_errors'] += 1
            self.stats['last_feed_match_error'] = code

    def match(self, params, stage):
        stage.pop('feed_match_error', None)
        try:
            if self.conn is None:
                if not self.path:
                    raise sqlite3.OperationalError('observation ledger has no readonly path')
                self.conn = get_readonly_connection(str(self.path))
                # This lookup measures feed lag; waiting for a ledger lock cannot improve proof.
                self.conn.execute('PRAGMA busy_timeout=0')
                self.conn.row_factory = None
            result = match_feed(self.conn, params[1], params[4],
                                stage.get('detected_epoch', time.time()))
            stage['feed_match_state'] = 'matched' if result[0] is not None else 'unmatched'
            return result
        except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as exc:
            self._unknown(stage, exc)
            self.close()
            return None, None

    def close(self, stage=None):
        if self.conn is not None:
            try:
                self.conn.close()
            except (sqlite3.Error, OSError) as exc:
                if stage is not None:
                    self._unknown(stage, exc)
            finally:
                self.conn = None


def _persist_observation(conn, params, sells, stage, readonly=None):
    """One idempotent observation, on its writer's thread; clocks belong to reception."""
    began = time.monotonic()
    if params is not None and (stage or {}).get("signal_feed") == "j7_hint_scan":
        stage["feed_match_state"] = "independent_scan"
        matched, stored = time.monotonic(), _store_detection(conn, params)
    elif params is not None:
        own_read = readonly is None
        if own_read:
            # Offline/script connections may point at another ledger. Never enrich from global DB.
            try:
                path = next((r[2] for r in conn.execute('PRAGMA database_list') if r[1] == 'main'), '')
            except sqlite3.Error:
                path = ''
            readonly = _FeedMatcher(path)
        try:
            feed_id, feed_delta = readonly.match(params, stage)
        finally:
            if own_read:
                readonly.close(stage)
        params = (*params[:10], feed_id, feed_delta, *params[12:])
        matched = time.monotonic()
        stored = _store_detection(conn, params)
    else:
        matched, stored = time.monotonic(), False
    for sale in sells:
        record_onchain_sell(conn, **sale)
    committed = time.monotonic()
    if stage:
        _stage_write({**stage, "feed_match_ms": _ms(began, matched),
                      "commit_ms": _ms(matched, committed),
                      "total_ms": _ms(stage.get("frame_mono"), committed)})
    return stored


# TERM/INT (SOL-3, Codex review 2026-10-06): the exit thread gives the writer this long to commit
# what is queued or in flight, records its final counters, and exits by the deadline regardless.
SHUTDOWN_DRAIN_S = 1.0
SHUTDOWN_DEADLINE_S = 3.0
_ACTIVE_WRITER = None          # the running watcher's writer, for the exit thread


def _rolled_back(conn):
    """`conn` rolled back after a failed write, or None (the next job reconnects) when it cannot
    even roll back: a broken connection is replaced, never left to end the writer thread (Gamma
    INFO on 5d5c00024)."""
    if conn is None:
        return None
    try:
        conn.rollback()
        return conn
    except Exception:  # noqa: BLE001 - a dead handle; the job's failure is already counted
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return None


def _note_write_failure(exc):
    """The writer's one log line for a failed write. It never raises: a broken stdout ended the
    writer thread here while the watcher kept receiving (Gamma INFO on SOL-3). `errors` counts it."""
    try:
        print("observation write failed: %s" % type(exc).__name__, flush=True)
    except Exception:  # noqa: BLE001 - the count is the record; the line is best effort
        pass


def _durable_detection(conn, params):
    """Writer-thread acknowledgement: a committed row has this exact source identity and proof."""
    if params is None:
        return False
    try:
        proof = json.loads(params[15]).get('source_initiation')
        if not isinstance(proof, dict) or not proof:
            return False
        identity = (params[0], params[3], params[1], params[4], params[16])
        if (proof.get('tx_hash'), proof.get('beneficiary'), proof.get('source_user_id'),
                proof.get('mint'), proof.get('block_number')) != identity or proof.get('payer') != params[3]:
            return False
        row = conn.execute('SELECT signature,address,source_user_id,mint,source_slot,source_valuation_json '
                           'FROM onchain_wallet_detections WHERE signature=?', (params[0],)).fetchone()
        return (row is not None and tuple(row[:5]) == identity
                and json.loads(row[5]).get('source_initiation') == proof)
    except (ValueError, TypeError, AttributeError, IndexError):
        return False


class DetectionWriter:
    """Bounded, single-owner SQLite writer. Socket reception only offers immutable jobs.

    ponytail: the queue is memory-only; restart loses outstanding observations. A durable spool
    earns its extra writes only if restart loss is measured. Overflow/retry/shutdown loss is counted.
    """
    def __init__(self, path, *, maxsize=256, retry_s=2.0):
        self.path, self.retry_s = path, float(retry_s)
        self._jobs, self._stop, self._closing = queue.Queue(maxsize=maxsize), threading.Event(), threading.Event()
        # Each counter has one writer: offered/overflow/shutdown_refused on reception, the others
        # on this worker.
        self.stats = dict(offered=0, overflow=0, written=0, duplicates=0, retries=0,
                          retry_exhausted=0, errors=0, shutdown_dropped=0, shutdown_refused=0,
                          sells_only=0, ack_errors=0, feed_match_errors=0,
                          last_feed_match_error=None, alive=False)
        self._thread = threading.Thread(target=self._run, name="onchain-observation-writer", daemon=True)
        self._thread.start()

    def offer(self, params, sells, stage, *, on_persisted=None, on_stored=None):
        if self._closing.is_set():
            self.stats["shutdown_refused"] += 1
            self._ack(on_persisted, False)
            return False
        try:
            self._jobs.put_nowait((params, tuple(sells), dict(stage), on_persisted, on_stored))
        except queue.Full:
            self.stats["overflow"] += 1
            self._ack(on_persisted, False)
            return False
        self.stats["offered"] += 1
        return True

    def _ack(self, callback, durable):
        if callback is not None:
            try:
                callback(durable)
            except Exception as exc:  # a callback cannot retry an already committed write
                self.stats['ack_errors'] += 1
                _note_write_failure(exc)

    def _run(self):
        conn = None
        readonly = _FeedMatcher(self.path, self.stats)
        self.stats["alive"] = True
        try:
            while not self._stop.is_set():
                try:
                    params, sells, stage, on_persisted, on_stored = self._jobs.get(timeout=.1)
                except queue.Empty:
                    continue
                deadline = time.monotonic() + self.retry_s
                committed = False
                while not self._stop.is_set():
                    try:
                        if conn is None:
                            conn = sqlite3.connect(self.path, timeout=.1)
                        stored = _persist_observation(conn, params, sells, stage, readonly)
                        # Every offered job ends in exactly one counter: offered == written +
                        # duplicates + sells_only + errors + retry_exhausted + shutdown_dropped
                        # once idle (Gamma, live 22:57Z: 3 sell-only commits were counted nowhere).
                        self.stats["sells_only" if params is None
                                   else "written" if stored else "duplicates"] += 1
                        committed = True
                        break
                    except sqlite3.OperationalError as exc:
                        conn = _rolled_back(conn)
                        if "locked" not in str(exc) and "busy" not in str(exc):
                            self.stats["errors"] += 1
                            _note_write_failure(exc)
                            break
                        self.stats["retries"] += 1
                        if time.monotonic() >= deadline:
                            self.stats["retry_exhausted"] += 1
                            break
                        self._stop.wait(.05)
                    except Exception as exc:
                        conn = _rolled_back(conn)
                        self.stats["errors"] += 1
                        _note_write_failure(exc)
                        break
                else:
                    self.stats["shutdown_dropped"] += 1
                durable = False
                if committed and on_persisted is not None:
                    try:
                        durable = _durable_detection(conn, params)
                    except Exception as exc:
                        self.stats['ack_errors'] += 1
                        _note_write_failure(exc)
                self._ack(on_persisted, durable)
                if committed:
                    self._ack(on_stored, stored)
                self._jobs.task_done()            # close() waits on in-flight work too
        finally:
            self.stats["alive"] = False
            while True:
                try:
                    _params, _sells, _stage, on_persisted, _on_stored = self._jobs.get_nowait()
                    self._ack(on_persisted, False)
                    self._jobs.task_done()
                    self.stats["shutdown_dropped"] += 1
                except queue.Empty:
                    break
            readonly.close()
            if conn is not None:
                conn.close()

    def close(self, drain_s=0.0):
        """Refuse new offers, give queued and in-flight jobs up to `drain_s` to commit, then stop:
        the worker counts whatever is left shutdown_dropped."""
        self._closing.set()
        end = time.monotonic() + max(0.0, float(drain_s))
        while self._jobs.unfinished_tasks and time.monotonic() < end:
            time.sleep(.01)
        self._stop.set()
        self._thread.join(.5)  # bounded by the worker's 100 ms SQLite wait, never a ledger lock


def _submit_observation(writer, params, sells, stage, *, on_persisted=None, on_stored=None):
    if isinstance(writer, DetectionWriter):
        return writer.offer(params, sells, stage, on_persisted=on_persisted, on_stored=on_stored)
    # Existing offline/script callers may supply their own connection.
    stored = _persist_observation(writer, params, sells, stage)
    if on_stored is not None:
        on_stored(stored)
    if on_persisted is not None:
        try:
            on_persisted(_durable_detection(writer, params))
        except Exception as exc:
            _note_write_failure(exc)
    return True


def _env_helius_url():
    """The ONE key this watcher uses: its own, else the first healthy research key.

    NO ROTATION (2026-09-26). The old candidate list went dedicated -> the singular
    MEMECOIN_RESEARCH_HELIUS_RPC_URL (its monthly quota ran out on 09-25) -> the Helius entries of
    MEMECOIN_SOLANA_RPC_POOL_URLS, which are FUNDED read keys. A WS 429 therefore walked this
    watcher onto a spent key and then onto the funded read lane. A 429 now backs off on this key.
    A key that is also a funded key refuses to start (credits are per account).
    """
    from sniper.services.memecoin_research_rpc import funded_overlap
    env = {}
    for line in (STATE_ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    # A KEY OF ITS OWN FIRST (2026-09-23): on the shared research key a leaders scan 429'd it.
    research = [u.strip() for u in env.get("MEMECOIN_RESEARCH_HELIUS_RPC_URLS", "").split(",")]
    url = next((u for u in [env.get("ONCHAIN_WATCH_HELIUS_RPC_URL", "")] + research[:1]
                if "helius" in u and "api-key=" in u), None)
    if url is None:
        raise SystemExit("no watcher or research Helius key in .env")
    if funded_overlap([url], env):
        raise SystemExit("the watcher key is also a funded key; refusing to start")
    return url


# RECONNECT BACKOFF (2026-09-28, Beta, Alpha's go). Every error used to double the wait up to 300 s,
# the rule written after a 429 retry loop burned ~7k shared-quota requests on 2026-08-21. On
# 2026-09-28 a keepalive ping timeout right after a relaunch, then six empty-error connect
# failures, took the wait to 128 s and left the watcher dark 21:17:42-21:26:25Z (8.7 min). Only a
# quota answer earns the long wait now; any other error retries within OTHER_BACKOFF_MAX_S, and
# never faster than the quota floor a 429 set, until a connect succeeds.
QUOTA_ERROR_MARKERS = ("429", "usage cap")
OTHER_BACKOFF_MAX_S = 8.0
QUOTA_BACKOFF_MAX_S = 300.0


def fresh_backoff():
    return {"other": 2.0, "quota": 0.0}


def reconnect_delay(state, err_text):
    """Seconds to wait before the next connect after an error whose text is `err_text`."""
    if any(marker in err_text for marker in QUOTA_ERROR_MARKERS):
        state["quota"] = min(state["quota"] * 2, QUOTA_BACKOFF_MAX_S) if state["quota"] else 2.0
        return state["quota"]
    delay = max(state["other"], state["quota"])
    state["other"] = min(state["other"] * 2, OTHER_BACKOFF_MAX_S)
    return delay


def _rotate_key(reason):
    """A WS 429 keeps this key; the reconnect loop's backoff is the whole response."""
    print(f"helius key {KEY[:8]}… rate limited on the socket ({reason}); backing off on own key",
          flush=True)


# Chosen at run() start, never at import: tests and report() import this module without a key.
KEY = HTTP_URL = WS_URL = None


def _select_key():
    global KEY, HTTP_URL, WS_URL
    from sniper.services.memecoin_solana_provider import infra_provider, provider_urls
    if infra_provider() == 'shyft':
        from sniper.services.memecoin_shyft_signal import live_config
        live_config()  # Validate before starting the quote/decode/writer threads.
        HTTP_URL = provider_urls('signal')[0]
        KEY = WS_URL = None
        return
    HTTP_URL = _env_helius_url()
    KEY = HTTP_URL.split("api-key=")[1].split("&")[0].strip()
    WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={KEY}"


# ONE KEY, ONE THROTTLE (2026-09-26). Every non-err frame started its own getTransaction on the
# executor, unbounded, and a 429 was retried 0.3 s later, tripling the load: 39% of calls came
# back 429 ('Too Many Requests', Retry-After: 1) and a 2.93 SOL buy was lost. Three in flight at
# most; a 429 sets a cooldown EVERY caller waits out; a quota 429 ('max usage') is never retried.
# ponytail: a semaphore, not a token bucket; add a per-second bucket if 429s persist after this.
_RPC_SLOTS = threading.BoundedSemaphore(3)
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN = [0.0]          # monotonic deadline; only ever raised
RETRY_AFTER_DEFAULT_S, RETRY_AFTER_MAX_S = 1.0, 5.0


def _retry_after_s(value):
    """Retry-After in seconds; missing, garbled, negative or HTTP-date form waits the default."""
    try:
        s = float(value)
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT_S
    return min(s, RETRY_AFTER_MAX_S) if math.isfinite(s) and s > 0 else RETRY_AFTER_DEFAULT_S


def _note_rate_limit(seconds):
    with _COOLDOWN_LOCK:
        _COOLDOWN[0] = max(_COOLDOWN[0], time.monotonic() + seconds)


def _cooldown_until():
    with _COOLDOWN_LOCK:
        return _COOLDOWN[0]


def _classify_429(exc):
    """'quota' (monthly, never retry), 'rate' (per second) or 'unknown' (no readable body)."""
    body = ""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read(512).decode("utf-8", "replace").lower()
        except Exception:
            body = ""
        if "max usage" in body:
            return "quota", None
        if exc.headers.get("Retry-After") is not None or "too many" in body:
            return "rate", exc.headers.get("Retry-After")
    return "unknown", None


# A buy moves an SPL balance, and only a token program can move one. A frame whose logs invoke
# neither token program at any depth cannot be a buy (145 of notdecu's 163 frames in 120 s invoked
# one unidentified program and nothing else). A program allowlist was rejected: it silently drops
# buys through any DEX not on the list. Logs that are missing, empty or cut ('Log truncated') are
# UNKNOWN and fetched.
TOKEN_PROGRAM_INVOKES = ("Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke",
                         "Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb invoke")


def frame_may_be_buy(logs):
    if not logs or any("Log truncated" in line for line in logs):
        return True
    return any(line.startswith(TOKEN_PROGRAM_INVOKES) for line in logs)


def _ms(a, b):
    """Milliseconds between two monotonic reads, or None when the start was not taken."""
    return None if a is None or b is None else round((b - a) * 1000.0, 2)


def _http_rpc(method, params, stats=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    if HTTP_URL is None:   # a caller outside run(), e.g. a test; run() selects before any thread
        _select_key()
    req = urllib.request.Request(HTTP_URL, data=body, headers={"content-type": "application/json"})
    from sniper.services.memecoin_solana_provider import infra_provider
    shyft = infra_provider() == 'shyft'
    attempts = 1 if shyft else 3
    for attempt in range(attempts):
        wait = _cooldown_until() - time.monotonic()
        if wait > 0:
            if shyft:
                if stats is not None:
                    stats['rpc_error'] = 'rpc_cooldown'
                return None
            time.sleep(wait)
            if stats is not None:
                stats["sleep_ms"] = stats.get("sleep_ms", 0.0) + wait * 1000.0
        try:
            with _RPC_SLOTS, urllib.request.urlopen(req, timeout=2.0 if shyft else 20) as r:
                result = json.loads(r.read().decode()).get("result")
            # getTransaction race: the WS fires at `confirmed` but getTransaction defaults to
            # `finalized`, so a just-confirmed tx returns null for ~1s. Retry a few times.
            # (Verified: with commitment=confirmed + retry, 5/6 live pump.fun buys parsed in ~1s;
            # without it, 0/25.) The caller passes commitment=confirmed in params.
            if stats is not None:
                stats["attempts"] = attempt + 1
            if result is None and method == "getTransaction" and attempt < attempts - 1:
                time.sleep(0.4)
                if stats is not None:
                    stats["sleep_ms"] = stats.get("sleep_ms", 0.0) + 400.0
                continue
            return result
        except Exception as exc:
            limited = "429" in str(exc)
            kind, retry_after = _classify_429(exc) if limited else (None, None)
            if stats is not None:
                stats["attempts"] = attempt + 1
                if limited:
                    stats["http_429"] = stats.get("http_429", 0) + 1
                    stats["http_429_kind"] = kind
            if kind == "quota" or attempt == attempts - 1:
                if shyft and limited:
                    _note_rate_limit(_retry_after_s(retry_after))
                return None
            if limited:
                _note_rate_limit(_retry_after_s(retry_after))   # waited at the loop top
                continue
            time.sleep(0.3)
            if stats is not None:
                stats["sleep_ms"] = stats.get("sleep_ms", 0.0) + 300.0


def load_watchlist(override_addrs=None):
    if override_addrs:
        # verification mode: watch a supplied set of active addresses (mechanism test)
        return {a: {"uid": a, "handle": a[:8], "status": "verify"} for a in override_addrs}
    conn = get_readonly_connection(str(DB))
    conn.row_factory = None  # tuple rows, as before: the helper defaults to sqlite3.Row and Row does not slice
    rows = conn.execute(
        "SELECT source_user_id, handle, address, verify_status FROM memecoin_wallet_addresses "
        "WHERE network_id='solana' AND verify_status IN ('fomoscan_confirmed','live_confirmed')"
    ).fetchall()
    conn.close()
    return {r[2]: {"uid": r[0], "handle": r[1], "status": r[3]} for r in rows}


# THE WATCH SET COMES FROM THE ADDRESS REGISTRY AND REFRESHES IN PROCESS (2026-09-27, registry step
# 3, docs/superpowers/specs/2026-09-26-solana-address-registry-design.md). The legacy loader above
# read memecoin_wallet_addresses ONCE per start: 24 addresses of which 9 were armed, while 6 armed
# wallets were not watched at all (Tekkerrss' $489 buy at 23:12:00Z on 09-26 was one of them).
# Now: the VERIFIED Solana addresses of profiles armed on solana, re-read every REFRESH_S on an
# executor thread, diffed against the live subscriptions. Unarmed detections cannot become funded
# entries, so they are not watched. A failed or empty read keeps the current set; a missing
# registry table falls back to the legacy loader.
REFRESH_S = 60.0
MAX_SUBS_DEFAULT = 100


def _max_subs():
    """Hard subscription cap. Read at call time: run() loads the env files first."""
    try:
        n = int(os.environ.get("MEMECOIN_ONCHAIN_WATCH_MAX_SUBS", MAX_SUBS_DEFAULT))
    except ValueError:
        return MAX_SUBS_DEFAULT
    return n if n > 0 else MAX_SUBS_DEFAULT


def registry_watchlist(conn):
    """{address: info} for the VERIFIED Solana addresses of profiles armed on solana (plus pump.fun
    ids), or None when the registry table does not exist (the legacy path). The same function
    as the slot lane's owners (armed_verified_owners), so the two lanes watch the same wallets."""
    from sniper.services.memecoin_address_registry import armed_verified_owners
    owners = armed_verified_owners(conn)
    if owners is None:
        return None
    return {addr: {"uid": uid, "handle": str(handle or uid[:8]), "status": "registry_verified"}
            for addr, (uid, handle) in owners.items()}


def desired_watchlist():
    """(watch, over_cap), or None to KEEP the current set (a failed read, or an empty one).

    The cap keeps the first MEMECOIN_ONCHAIN_WATCH_MAX_SUBS addresses in address order and COUNTS
    the rest; the watchdog's drift check runs this same function, so a cap never reads as drift."""
    try:
        conn = get_readonly_connection(str(DB))
        try:
            watch = registry_watchlist(conn)
        finally:
            conn.close()
        if watch is None:
            watch = load_watchlist()
    except Exception as exc:  # noqa: BLE001 - a refresh that cannot read keeps what it has
        print(f"watch-set refresh failed, keeping the current set: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return None
    if not watch:
        return None
    keep = sorted(watch)[:_max_subs()]
    return {a: watch[a] for a in keep}, len(watch) - len(keep)


class Subscriptions:
    """logsSubscribe bookkeeping for ONE socket. Pure: `plan` returns the requests to send.

    A frame is looked up by its subscription id and carries that subscription's own (addr, info),
    so an address removed from the desired set keeps resolving until its unsubscribe is ACKED; a
    frame racing the removal is processed, never a KeyError that kills the loop."""

    def __init__(self):
        self.by_sub = {}        # acked subscription id -> (addr, info)
        self.addr_sub = {}      # addr -> acked subscription id
        self.requests = {}      # request id -> ("sub", addr, info) | ("unsub", addr, sub_id)
        self.failed = 0
        self._next = 0

    def _id(self):
        self._next += 1
        return self._next

    def plan(self, desired):
        out = []
        in_flight = {(r[0], r[1]) for r in self.requests.values()}
        for addr, info in desired.items():
            if addr in self.addr_sub:
                self.by_sub[self.addr_sub[addr]] = (addr, info)      # a renamed handle, same sub
            elif ("sub", addr) not in in_flight:
                rid = self._id()
                self.requests[rid] = ("sub", addr, info)
                out.append({"jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                            "params": [{"mentions": [addr]}, {"commitment": "confirmed"}]})
        for addr, sub_id in list(self.addr_sub.items()):
            if addr not in desired and ("unsub", addr) not in in_flight:
                rid = self._id()
                self.requests[rid] = ("unsub", addr, sub_id)
                out.append({"jsonrpc": "2.0", "id": rid, "method": "logsUnsubscribe",
                            "params": [sub_id]})
        return out

    def on_reply(self, msg):
        """True when `msg` answers one of our requests (acked or failed)."""
        req = self.requests.pop(msg.get("id"), None) if "id" in msg else None
        if req is None:
            return False
        kind, addr, extra = req
        result = msg.get("result")
        if kind == "sub" and isinstance(result, int) and not isinstance(result, bool):
            self.by_sub[result] = (addr, extra)
            self.addr_sub[addr] = result
        elif kind == "unsub" and result is True:
            self.by_sub.pop(extra, None)
            if self.addr_sub.get(addr) == extra:
                del self.addr_sub[addr]
        else:
            self.failed += 1           # an error reply: the next plan() asks again
        return True

    def lookup(self, sub_id):
        return self.by_sub.get(sub_id)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _receipt_utc(frame_mono):
    """Corrected receipt UTC, preserving time already spent fetching/decoding the receipt."""
    now = time.monotonic()
    if frame_mono is not None and (isinstance(frame_mono, bool)
            or not isinstance(frame_mono, (int, float)) or not math.isfinite(frame_mono)
            or frame_mono < 0 or frame_mono > now):
        return None
    epoch = real_utc_now() - (0.0 if frame_mono is None else now - frame_mono)
    if not math.isfinite(epoch):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def classify_buy(tx, wallet):
    """Return (mint, token_delta, sol_spent, usdc_spent) for a buy, else None.

    A buy = the wallet's balance of some non-quote mint INCREASED while it spent SOL or USDC.
    Airdrops/transfers-in (no spend) are excluded."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    pre_tok = {(b.get("owner"), b.get("mint")): float(b["uiTokenAmount"]["uiAmount"] or 0)
               for b in meta.get("preTokenBalances", []) if b.get("owner")}
    got = None
    for b in meta.get("postTokenBalances", []):
        owner, mint = b.get("owner"), b.get("mint")
        if owner != wallet or mint in QUOTE_MINTS or not mint:
            continue
        delta = float(b["uiTokenAmount"]["uiAmount"] or 0) - pre_tok.get((owner, mint), 0.0)
        if delta > 0 and (got is None or delta > got[1]):
            got = (mint, delta)
    if not got:
        return None
    # did the wallet SPEND? native SOL drop (fee+buy) or USDC token drop.
    # Balance arrays index the FULL ordered account list: static keys, then ALT loaded
    # writable, then loaded readonly. pump.fun swaps are versioned txns with ALTs, so the
    # buyer is often in loadedAddresses — indexing only accountKeys returned sol_spent=0 and
    # wrongly rejected real buys (verified on a live 1.01-SOL buy, 2026-08-20).
    keys = [k.get("pubkey") if isinstance(k, dict) else k
            for k in tx["transaction"]["message"]["accountKeys"]]
    loaded = meta.get("loadedAddresses") or {}
    all_keys = keys + loaded.get("writable", []) + loaded.get("readonly", [])
    sol_spent = 0.0
    if wallet in all_keys:
        i = all_keys.index(wallet)
        pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre_b) and i < len(post_b):
            sol_spent = max(0.0, (pre_b[i] - post_b[i]) / 1e9)
    usdc_spent = max(0.0, pre_tok.get((wallet, USDC), 0.0)
                     - next((float(b["uiTokenAmount"]["uiAmount"] or 0)
                             for b in meta.get("postTokenBalances", [])
                             if b.get("owner") == wallet and b.get("mint") == USDC), 0.0))
    wsol_spent = pre_tok.get((wallet, WSOL), 0.0) - sum(
        float(b["uiTokenAmount"]["uiAmount"] or 0) for b in meta.get("postTokenBalances", [])
        if b.get("owner") == wallet and b.get("mint") == WSOL)
    if sol_spent <= 0.0000001 and usdc_spent <= 0 and wsol_spent <= 0:
        return None                      # received tokens without spending -> not a buy
    return got[0], got[1], sol_spent, usdc_spent


def classify_sell(tx, wallet):
    """[(mint, sold_atomic, pre_atomic, post_atomic)] for each coin the wallet SOLD, else [].

    Source-sell mirror backup (2026-09-27). A sell = the wallet's own atomic balance of a non-quote
    mint DECREASED while it RECEIVED proceeds: lamports up once its fee is added back, or more WSOL
    or USDC. A transfer out with no proceeds is not a sell. Exact atomic amounts from the token
    balances, summed over every account the wallet owns, so the share (pre - post) / pre is exact.

    TWO PROOFS, since review S27-01 (2026-09-27): transferring every token away and closing the
    empty account refunds its rent, which read as proceeds and made a 100% sale out of no swap.
      * Proceeds exclude refunds: the pre-transaction lamports of every token account the wallet
        owned and the transaction closed are subtracted (exact, so Token-2022 rent and an unwrapped
        WSOL balance come out too).
      * The coin went to a counterparty: some other owner gained that mint and either gave
        something up in the same transaction (another token, or lamports net of its fee) or is a
        program address (off the ed25519 curve). A pool pays; a wallet receiving a transfer does
        not. Manifest (behind DFlow) holds base and quote in vaults with different PDA owners,
        which is why a program address alone is enough.
    Checked 2026-09-27 on 740 real sells of 42 registry owners (none lost) and 1,008 old-rule sells
    in random pump/PumpSwap/Jupiter/DFlow traffic (18 dropped: 16 pool-side legs, and 2 wallet
    sells worth nothing to the seller, a DFlow route paying another address and a dead pump coin
    that paid 1,301 lamports against its rent). Not handled: proceeds paid to another wallet.
    """
    from solders.pubkey import Pubkey
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    pre_rows, post_rows = meta.get("preTokenBalances") or [], meta.get("postTokenBalances") or []

    unknown = set()
    def by_owner_mint(rows):
        out = {}
        for b in rows:
            if b.get("owner") and b.get("mint"):
                key = (b["owner"], b["mint"])
                amount = b.get("uiTokenAmount")
                raw = amount.get("amount") if isinstance(amount, dict) else None
                if (isinstance(raw, bool) or not isinstance(raw, (str, int))
                        or len(str(raw)) > 20 or not str(raw).isascii() or not str(raw).isdigit()
                        or int(raw) >= 2**64):
                    unknown.add(key)
                    continue
                out[key] = out.get(key, 0) + int(raw)
        return out
    pre, post = by_owner_mint(pre_rows), by_owner_mint(post_rows)
    if unknown:
        print("source-sell decode: atomic balance unknown for %d owner/mint pair(s)" % len(unknown),
              file=sys.stderr, flush=True)
        for key in unknown:
            pre.pop(key, None)
            post.pop(key, None)
    delta = {k: post.get(k, 0) - pre.get(k, 0) for k in set(pre) | set(post)}
    keys = [k.get("pubkey") if isinstance(k, dict) else k
            for k in tx["transaction"]["message"]["accountKeys"]]
    loaded = meta.get("loadedAddresses") or {}
    all_keys = keys + loaded.get("writable", []) + loaded.get("readonly", [])
    index = {k: i for i, k in reversed(list(enumerate(all_keys)))}
    pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []

    def lamports_in(i):
        if i is None or i >= len(pre_b) or i >= len(post_b):
            return 0
        return post_b[i] - pre_b[i] + (int(meta.get("fee") or 0) if i == 0 else 0)
    still_open = {b.get("accountIndex") for b in post_rows}
    refunds = sum(max(0, pre_b[i] - post_b[i]) for b in pre_rows
                  if b.get("owner") == wallet and (i := b.get("accountIndex")) is not None
                  and i not in still_open and i < len(pre_b) and i < len(post_b))
    quote_in = any(delta.get((wallet, q), 0) > 0 for q in (USDC, WSOL))
    if lamports_in(index.get(wallet)) - refunds <= 0 and not quote_in:
        return []

    def counterparty(owner):
        return (any(d < 0 for (o, _mint), d in delta.items() if o == owner)
                or lamports_in(index.get(owner)) < 0
                or not Pubkey.from_string(owner).is_on_curve())
    return [(mint, -d, pre.get((wallet, mint), 0), post.get((wallet, mint), 0))
            for (owner, mint), d in delta.items()
            if owner == wallet and mint not in QUOTE_MINTS and d < 0
            and any(counterparty(o) for (o, m2), got in delta.items()
                    if m2 == mint and got > 0 and o != wallet)]


def match_feed(conn, uid, mint, detected_epoch, window_s=180):
    """The memecoin_events buy row for this (uid, mint) nearest the detection, and how much
    LATER the fomo feed received it vs our on-chain detection."""
    rows = conn.execute(
        "SELECT fomo_event_id, received_at_normalized, received_at FROM memecoin_events "
        "WHERE source_user_id=? AND mint=? AND side='buy' "
        "ORDER BY id DESC LIMIT 25", (uid, mint)).fetchall()
    best = None
    for eid, ran, ra in rows:
        ts = ran or ra
        try:
            et = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        # NEAREST, not earliest (fixed 2026-09-08). `et < best[1]` picked the OLDEST event in the
        # window, which is systematically before the detection, so the reported lag was pulled
        # negative and the feed looked faster than it is. Measured on 503 stored detections: 61 of
        # 308 matched rows (19.8%) chose a different event, biased a median -48s. That is the same
        # magnitude as the -41s median in `feed_lag_vs_onchain_s`, so this rule was manufacturing
        # the "the feed beats the chain" reading it was built to test.
        if abs(et - detected_epoch) <= window_s and (
                best is None or abs(et - detected_epoch) < abs(best[1] - detected_epoch)):
            best = (eid, et)
    if not best:
        return None, None
    return best[0], best[1] - detected_epoch   # + => feed later than us (we won)


async def process_signature(sig, wallet, meta_info, wdb, frame_mono=None, *, sol_usd=None,
                            confirmed_seen=None):
    if confirmed_seen is not None and confirmed_seen(sig, wallet, meta_info['uid']):
        return
    t_task = time.monotonic()
    stats = {"attempts": 0, "sleep_ms": 0.0, "http_429": 0}
    t_issued = time.monotonic()
    tx = await asyncio.get_event_loop().run_in_executor(
        None, _http_rpc, "getTransaction",
        # Version 1 (2026-09-26): bot routers (FLASHX, term9) send v1 transactions, and a
        # request capped at 0 is refused with -32015, so those buys were never classified.
        [sig, {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed",
               "commitment": RPC_COMMITMENT}], stats)
    t_returned = time.monotonic()
    if not tx:
        _stage_write({"ts": time.time(), "addr": wallet, "sig": sig, "outcome": "no_tx", "commitment": RPC_COMMITMENT,
                      "queue_ms": _ms(frame_mono, t_task),
                      "rpc_ms": _ms(t_issued, t_returned), **stats})
        return
    return process_transaction(sig, wallet, meta_info, wdb, tx=tx, frame_mono=frame_mono,
        sol_usd=sol_usd, stats=stats, t_task=t_task, t_issued=t_issued, t_returned=t_returned,
        commitment=RPC_COMMITMENT)


def _no_row_stats():
    from sniper.services.memecoin_fill_timing import watcher_no_row_stats
    return watcher_no_row_stats()


def _no_row(sig, wallet, meta_info, signal_feed, reason):
    """A classified buy that leaves no detection row gets a named decision (memecoin_watcher_no_row in the
    timing file): before 2026-10-16 such a frame was judged by nothing. Never raises."""
    from sniper.services.memecoin_fill_timing import note_watcher_no_row
    note_watcher_no_row(sig, wallet, (meta_info or {}).get("uid"), signal_feed, reason)


def process_transaction(sig, wallet, meta_info, wdb, *, tx, frame_mono=None, sol_usd=None,
                        stats=None, t_task=None, t_issued=None, t_returned=None,
                        detected_at=None, detected_epoch=None, signal_feed="helius",
                        commitment="confirmed", require_initiation=False, on_persisted=None,
                        buy_only=False, on_stored=None):
    """Process the same receipt from HTTP or a confirmed full-metadata stream; no reads.

    Stream parsing must prove initiation before inserting: an unsupported instruction
    cannot win the unique signature row and suppress the richer HTTP fallback.
    """
    t_task = time.monotonic() if t_task is None else t_task
    stats = {} if stats is None else stats
    if commitment not in ('confirmed', 'finalized'):
        _stage_write({'ts': time.time(), 'addr': wallet, 'sig': sig,
                      'outcome': 'receipt_commitment_unknown', **stats})
        return False
    signatures = (tx.get("transaction") or {}).get("signatures") or []
    if signatures and signatures[0] != sig:
        _stage_write({"ts": time.time(), "addr": wallet, "sig": sig, "outcome": "transaction_identity_mismatch", **stats})
        return
    t_parsed = time.monotonic()
    buy = classify_buy(tx, wallet)
    sells = [dict(signature=sig, source_user_id=meta_info["uid"],
                  handle=meta_info["handle"], address=wallet, mint=mint, sold_atomic=sold,
                  pre_atomic=before, post_atomic=after, slot=tx.get("slot"),
                  block_time=tx.get("blockTime"), detected_at=now_iso(), created_at=now_iso())
             for mint, sold, before, after in (classify_sell(tx, wallet) if signatures and not buy_only else ())]
    stage = {"ts": time.time(), "addr": wallet, "sig": sig, "commitment": commitment, "signal_feed": signal_feed,
             "queue_ms": _ms(frame_mono, t_task), "rpc_ms": _ms(t_issued, t_returned),
             "frame_mono": frame_mono, **stats}
    if not buy:
        stage["outcome"] = "sell" if sells else "not_a_buy"
        if sells:
            _submit_observation(wdb, None, sells, stage)
        else:
            _stage_write(stage)
        return
    mint, delta, sol_spent, usdc_spent = buy
    bt = tx.get("blockTime") if signatures else None
    det_epoch = time.time() if detected_epoch is None else detected_epoch
    detected_at = _receipt_utc(frame_mono) if detected_at is None else detected_at
    try:
        arrival = datetime.fromisoformat(detected_at.replace('Z', '+00:00'))
        if arrival.tzinfo is None:
            raise ValueError('naive receipt')
        arrival_epoch = arrival.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        _stage_write({**stage, 'outcome': 'receipt_clock_unknown'})
        _no_row(sig, wallet, meta_info, signal_feed, 'receipt_clock_unknown')
        return False
    valuation = (value_source_buy(tx, wallet, sol_usd=sol_usd, now=det_epoch) if signatures else
                 dict(source_size_usd=None, source_price=None, basis="unknown",
                      source_clock_provenance="unverified_receipt",
                      unknown_reason="missing_transaction_signature"))
    if signal_feed.startswith("shyft_confirmed:"):
        valuation["source_clock_provenance"] = "confirmed_shyft_grpc"
    elif signal_feed == "helius_enhanced_confirmed":
        valuation["source_clock_provenance"] = "confirmed_helius_websocket"
    if signal_feed == "j7_hint_scan":
        valuation.update(backfill=True, j7_native_failure_backup=True)
    valuation["signature"] = sig
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    initiation = solana_receipt_initiation(tx, wallet, valuation)
    if require_initiation and initiation is None:
        _stage_write({**stage, "outcome": "source_initiation_unknown"})
        _no_row(sig, wallet, meta_info, signal_feed, "no_initiation:" + str(
            valuation.get("unknown_reason") or valuation.get("event_unknown_reason") or "unknown"))
        return False
    valuation["source_initiation"] = (dict(initiation, source_user_id=meta_info['uid'])
                                      if initiation is not None else None)
    if valuation.get("mint"):
        # The bound swap output wins over the heuristic's largest balance increase.
        mint, delta = valuation["mint"], valuation["token_delta"]
    source_size_usd = valuation["source_size_usd"]
    # The route valuation runs first and keeps the event path's reason in event_unknown_reason; the
    # wallet-delta fallback applies only when neither the event nor the route produced a size.
    if usdc_spent > 0 and source_size_usd is None and "missing_buy_event" in (
            valuation.get("unknown_reason"), valuation.get("event_unknown_reason")):
        # Retain the existing USDC observation, explicitly labeled as a wallet balance delta.
        source_size_usd = usdc_spent
        valuation.update(basis="owner_usdc_balance_delta", source_size_usd=usdc_spent)
    source_price = source_size_usd / delta if source_size_usd is not None else None
    valuation["source_price"] = source_price
    params = (sig, meta_info["uid"], meta_info["handle"], wallet, mint, delta, sol_spent, bt,
              detected_at, (arrival_epoch - bt) if bt is not None else None, None, None, now_iso(),
              source_size_usd, source_price, json.dumps(valuation, separators=(",", ":")), tx.get("slot"))
    stage.update(mint=mint, outcome="row", block_time=bt, detected_epoch=arrival_epoch,
                 parse_ms=_ms(t_returned, t_parsed))
    accepted = _submit_observation(wdb, params, sells, stage, on_persisted=on_persisted, on_stored=on_stored)
    if not accepted:
        _stage_write({**stage, "outcome": "writer_overflow"})
        _no_row(sig, wallet, meta_info, signal_feed, "writer_overflow")
    print(f"BUY  {meta_info['handle']:<14} {mint[:12]} sol_spent={sol_spent:.3f} "
          f"[{meta_info['status']}] detection offered", flush=True)

    return accepted


def _shyft_receipt(update, owners, *, head_slot, recv_monotonic, head_monotonic):
    """One recovery read on the decode worker, only for an exact fresh confirmed identity."""
    from sniper.services.memecoin_shyft_confirmed import confirmed_transaction, receipt_signers
    tx = confirmed_transaction(update, owners, head_slot=head_slot,
        recv_monotonic=recv_monotonic, head_monotonic=head_monotonic)
    if tx is not None:
        return tx
    if (not update.HasField('transaction') or head_slot is None or head_monotonic is None
            or not 0 <= recv_monotonic - head_monotonic <= 2.0
            or not head_slot - 2 <= update.transaction.slot <= head_slot + 2):
        return None
    info = update.transaction.transaction
    if (info.is_vote or info.HasField('meta') and info.meta.HasField('err')
            or not info.HasField('transaction') or len(info.signature) != 64):
        return None
    msg, signatures = info.transaction.message, info.transaction.signatures
    n = msg.header.num_required_signatures
    if (not msg.HasField('header') or not 1 <= n <= len(msg.account_keys)
            or len(signatures) != n or signatures[0] != info.signature
            or any(len(s) != 64 for s in signatures)
            or any(len(k) != 32 for k in msg.account_keys)):
        return None
    import base58
    signed = tuple(base58.b58encode(k).decode() for k in msg.account_keys[:n])
    if not set(signed).intersection(owners):
        return None
    sig = base58.b58encode(info.signature).decode()
    recovered = _http_rpc('getTransaction', [sig, {'maxSupportedTransactionVersion': 1,
        'encoding': 'jsonParsed', 'commitment': 'confirmed'}])
    if (not isinstance(recovered, dict) or recovered.get('slot') != update.transaction.slot
            or not isinstance(recovered.get('meta'), dict)
            or recovered['meta'].get('err', 'unknown') is not None
            or (recovered.get('transaction') or {}).get('signatures') !=
                [base58.b58encode(s).decode() for s in signatures]):
        return None
    # Both HTTP shapes share the worker's signer boundary; ordered identity binds to the wire.
    if receipt_signers(recovered) != signed:
        return None
    return recovered


def _old_shyft_watchers():
    from scripts.memecoin_runtime_verify import _watcher_rows, _process_start_time
    return tuple((pid, path, _process_start_time(pid))
                 for pid, path in _watcher_rows(STATE_ROOT) if pid != os.getpid())


def _old_shyft_watchers_alive(identities):
    from scripts.memecoin_runtime_verify import _process_start_time
    for pid, path, birth in identities:
        current = _process_start_time(pid)
        if (current is not None and (birth is None or current == birth)) or (
                current is None and Path('/proc', str(pid)).exists()):
            return True
    return False


def start_confirmed_feed(owners_fn, writer, cache, *, old_watchers=None, old_alive_fn=None):
    """Independent confirmed streams; configuration failures retain existing receipt fallback."""
    from sniper.services.memecoin_shyft_signal import live_config
    from sniper.services import memecoin_helius_evidence as helius
    from sniper.services.memecoin_solana_provider import infra_provider
    shyft_only = infra_provider() == 'shyft'
    status = dict(feeds={}, last_error=None)
    config = helius_url = None
    try:
        config = live_config()
    except ValueError as exc:
        if shyft_only:
            raise
        status['last_error'] = str(exc)  # live_config emits fixed codes, never credentials
    if not shyft_only:
        try:
            helius_url = helius.live_config()
        except ValueError:
            status['helius_error'] = 'helius_evidence_config_invalid'
    if config is None and helius_url is None:
        return dict(stop=None, worker=None, tasks=[], status=status)
    from sniper.services.memecoin_shyft_confirmed import ReceiptWorker, run_feed
    token, urls = config if config is not None else (None, ())
    stop = threading.Event()
    def consume(tx, owner, info, receipt):
        # Either decoder may finish after withdrawal or address reassignment. A queued roster
        # snapshot describes reception; only the current roster may offer a new detection.
        if dict(owners_fn()).get(owner) != info:
            return False
        detected_at = _receipt_utc(receipt['recv_monotonic'])
        if detected_at is None:
            return False
        accepted = process_transaction(tx['transaction']['signatures'][0], owner, info, writer, tx=tx,
            frame_mono=receipt['recv_monotonic'], detected_at=detected_at,
            detected_epoch=receipt['recv_epoch'], signal_feed=receipt['signal_feed'],
            commitment='confirmed', sol_usd=cache.get('snapshot'), require_initiation=True,
            on_persisted=receipt.get('on_persisted'))
        return None if accepted is True and isinstance(writer, DetectionWriter) else accepted is True
    worker = (ReceiptWorker(consume, decoder=_shyft_receipt) if shyft_only else
              ReceiptWorker(consume) if urls
              else ReceiptWorker(consume, decoder=helius.confirmed_transaction))
    status['worker'] = worker.stats
    tasks = []
    def start_url(url):
        from urllib.parse import urlsplit
        feed = status['feeds'][urlsplit(url).hostname] = {}
        tasks.append(asyncio.create_task(run_feed(url, token, owners_fn, worker, feed, stop)))
    if shyft_only:
        from sniper.services.memecoin_real_runner import _provider_role_identity
        status['http_role'] = _provider_role_identity(HTTP_URL) if HTTP_URL is not None else None
        old_watchers = _old_shyft_watchers() if old_watchers is None else old_watchers
        old_alive_fn = old_alive_fn or _old_shyft_watchers_alive
        waiting = bool(old_watchers and old_alive_fn(old_watchers))
        status['handover'] = dict(phase='primary' if waiting else 'full',
                                 old_watcher_count=len(old_watchers))
        for url in urls[:1] if waiting else urls:
            start_url(url)
        if waiting:
            async def finish_handover():
                while not stop.is_set() and old_alive_fn(old_watchers):
                    await asyncio.sleep(.25)
                if not stop.is_set():
                    for url in urls[1:]:
                        start_url(url)
                    status['handover']['phase'] = 'full'
            tasks.append(asyncio.create_task(finish_handover()))
    else:
        for url in urls:
            start_url(url)
    if helius_url:
        feed = status['feeds']['helius_enhanced_confirmed'] = {}
        evidence_worker = (ReceiptWorker(consume, decoder=helius.confirmed_transaction) if urls else worker)
        feed['worker'] = evidence_worker.stats
        tasks.append(asyncio.create_task(helius.run_feed(helius_url, owners_fn, evidence_worker, feed, stop)))
    else:
        evidence_worker = None
    workers = [worker] + ([evidence_worker] if evidence_worker is not None and evidence_worker is not worker else [])
    def has_receipt(*args):
        return any(w.has_receipt(*args) for w in workers)
    return dict(stop=stop, worker=worker, workers=workers,
                has_receipt=has_receipt, tasks=tasks, status=status)


class J7HintScanner:
    """Opaque IDs wake one bounded independent owner scan; they never identify a receipt."""
    def __init__(self, path, writer, cache):
        self.path, self.writer, self.cache = path, writer, cache
        self.cursor, self.pending, self.last_owner_scan = None, {}, {}
        self.last_scan_mono = None
        self._stop, self._thread, self._stats_lock = threading.Event(), None, threading.Lock()
        self.stats = dict(enabled=True, alive=False, ready=False, hints=0, coalesced=0,
            untrusted=0, stale=0, overflow=0, scans=0, rpc_calls=0, rpc_unavailable=0,
            transactions=0, discovered_new=0, already_present=0, errors=0,
            last_poll=None, last_scan=None, last_error=None)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="j7-independent-owner-scan", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.5)

    def snapshot(self):
        return dict(self.stats, pending=len(self.pending),
                    alive=self._thread is not None and self._thread.is_alive())

    @staticmethod
    def _fresh(epoch, now):
        return type(epoch) in (int, float) and math.isfinite(epoch) and 0 <= now - epoch <= 45

    @staticmethod
    def _epoch(value):
        try:
            stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            return stamp.timestamp() if stamp.tzinfo is not None else None
        except (ValueError, TypeError, OverflowError):
            return None

    def _stored(self, inserted):
        with self._stats_lock:
            self.stats['discovered_new' if inserted else 'already_present'] += 1

    def tick(self, conn):
        now, mono = time.time(), time.monotonic()
        self.stats['last_poll'] = now
        if self.cursor is None:
            self.cursor = conn.execute('SELECT COALESCE(MAX(id),0) FROM memecoin_events').fetchone()[0]
            self.stats['ready'] = True
            return
        owners = registry_watchlist(conn) or {}  # Never fall back to unarmed legacy identities.
        by_uid = {}
        for address, info in owners.items():
            by_uid.setdefault(info['uid'], []).append(address)
        rows = conn.execute('SELECT id,source_user_id,side,network_id,delivery_lane,created_at,'
            'received_at_normalized,received_at FROM memecoin_events WHERE id>? ORDER BY id LIMIT 64',
            (self.cursor,)).fetchall()
        for row in rows:
            self.cursor = row[0]
            if row[2] != 'buy' or row[3] != 'solana' or row[4] not in ('j7_wallets', 'j7_pump'):
                continue
            self.stats['hints'] += 1
            claimed, received = self._epoch(row[5]), self._epoch(row[6] or row[7])
            if not self._fresh(claimed, now) or not self._fresh(received, now):
                self.stats['stale'] += 1
                continue
            addresses = by_uid.get(row[1], ())
            if len(addresses) != 1:
                self.stats['untrusted'] += 1
                continue
            owner = addresses[0]
            if owner in self.pending or mono - self.last_owner_scan.get(owner, -1e30) < 5:
                self.stats['coalesced'] += 1
            elif len(self.pending) >= 16:
                self.stats['overflow'] += 1
            else:
                self.pending[owner] = (dict(owners[owner]), claimed, received)
        if (not self.pending or self.last_scan_mono is not None and mono - self.last_scan_mono < 2
                or _cooldown_until() > mono):
            return
        owner = next(iter(self.pending))
        info, claimed, received = self.pending.pop(owner)
        if owners.get(owner) != info or not self._fresh(claimed, now) or not self._fresh(received, now):
            self.stats['untrusted'] += 1
            return
        self.last_scan_mono = self.last_owner_scan[owner] = mono
        self.last_owner_scan = {o: t for o, t in self.last_owner_scan.items() if o in owners}
        self.stats['scans'] += 1
        self.stats['last_scan'] = now
        self.stats['last_error'] = None
        self.stats['rpc_calls'] += 1
        page = _http_rpc('getSignaturesForAddress', [owner, {'limit': 5, 'commitment': 'confirmed'}])
        if not isinstance(page, list):
            self.stats['rpc_unavailable'] += 1
            self.stats['last_error'] = 'signature_page_unavailable'
            return
        from sniper.services.memecoin_links import _public_id
        fetched, seen = 0, set()
        for row in page[:5]:
            if self._stop.is_set() or fetched >= 3:
                break
            if (not isinstance(row, dict) or row.get('confirmationStatus') not in ('confirmed', 'finalized')
                    or 'err' not in row or row['err'] is not None
                    or type(row.get('blockTime')) is not int
                    or not self._fresh(row['blockTime'], time.time())):
                continue
            sig = row.get('signature')
            if not isinstance(sig, str) or not _public_id(sig, 'solana', 64) or sig in seen:
                continue
            seen.add(sig)
            if conn.execute('SELECT 1 FROM onchain_wallet_detections WHERE signature=?', (sig,)).fetchone():
                self._stored(False)
                continue
            if (registry_watchlist(conn) or {}).get(owner) != info:
                break
            fetched += 1
            self.stats['rpc_calls'] += 1
            tx = _http_rpc('getTransaction', [sig, {'maxSupportedTransactionVersion': 1,
                                                  'encoding': 'jsonParsed', 'commitment': 'confirmed'}])
            if not isinstance(tx, dict):
                self.stats['rpc_unavailable'] += 1
                self.stats['last_error'] = 'receipt_unavailable'
                continue
            if (self._stop.is_set() or not self._fresh(claimed, time.time())
                    or not self._fresh(received, time.time())
                    or type(tx.get('blockTime')) is not int or not self._fresh(tx['blockTime'], time.time())
                    or (tx.get('transaction') or {}).get('signatures', [None])[:1] != [sig]
                    or (registry_watchlist(conn) or {}).get(owner) != info):
                continue
            self.stats['transactions'] += 1
            process_transaction(sig, owner, info, self.writer, tx=tx, frame_mono=time.monotonic(),
                sol_usd=self.cache.get('snapshot'), detected_at=now_iso(), detected_epoch=time.time(),
                signal_feed='j7_hint_scan', commitment='confirmed', require_initiation=True,
                buy_only=True, on_stored=self._stored)

    def _run(self):
        conn = None
        self.stats['alive'] = True
        try:
            while not self._stop.is_set():
                try:
                    if conn is None:
                        conn = get_readonly_connection(str(self.path))
                        conn.execute('PRAGMA busy_timeout=0')
                    self.tick(conn)
                except Exception as exc:
                    self.stats['errors'] += 1
                    self.stats['last_error'] = type(exc).__name__
                    if conn is not None:
                        conn.close()
                        conn = None
                self._stop.wait(.25)
        finally:
            self.stats['alive'] = False
            if conn is not None:
                conn.close()


def start_j7_hint_scanner(writer, cache):
    # run() loads saved env after db_memecoin has already imported the settings module.
    if not (os.getenv('MEMECOIN_FUNDED_ONLY', '0').strip().lower() in {'1', 'true', 'yes'}
            and os.getenv('MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', '0') == '1'
            and os.getenv('MEMECOIN_SOLANA_INFRA_PROVIDER', 'legacy').strip().lower() == 'shyft'
            and os.getenv('MEMECOIN_SIGNAL_SOURCE', 'fomo').strip().lower() == 'j7'):
        return None
    scanner = J7HintScanner(DB, writer, cache)
    scanner.start()
    return scanner


def _capture_sol_usd():
    from sniper.execution.solana_real_seams import ROLE_CAPTURE, jupiter_role, _quote_with_fallback
    # ContextVar scoped inside the worker; never switches another thread's funded role.
    with jupiter_role(ROLE_CAPTURE):
        quote = _quote_with_fallback(WSOL, USDC, 1_000_000_000, 50, None) or {}
    if (quote.get("inputMint") != WSOL or quote.get("outputMint") != USDC
            or str(quote.get("inAmount")) != "1000000000"):
        return None
    try:
        price = float(quote["outAmount"]) / 1e6
        return price if math.isfinite(price) and price > 0 else None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


async def _refresh_sol_usd(cache):
    while True:
        observed_at = time.time()  # request start: latency cannot make an old quote look young
        try:
            price = await asyncio.to_thread(_capture_sol_usd)
            if price is None or not math.isfinite(price) or price <= 0:
                raise ValueError("invalid_quote")
            cache["snapshot"] = (price, observed_at)
        except Exception as exc:
            # Keep the prior timestamp; readers expire it. Never log credential-bearing URLs.
            print(f"SOL/USD refresh unavailable: {type(exc).__name__}", flush=True)
        await asyncio.sleep(10)


async def run(duration_s=None, override_addrs=None):
    from sniper.util.envfiles import load_env_files
    load_env_files(str(STATE_ROOT))
    _select_key()
    cache = {}
    refresh = asyncio.create_task(_refresh_sol_usd(cache))
    try:
        return await _run(duration_s, override_addrs, cache)
    finally:
        refresh.cancel()
        await asyncio.gather(refresh, return_exceptions=True)


async def _run(duration_s, override_addrs, cache):
    over_cap, refreshed_at = 0, None
    got = None if override_addrs else desired_watchlist()
    if got is not None:
        (watch, over_cap), refreshed_at = got, now_iso()
    else:
        watch = load_watchlist(override_addrs)
    if not watch:
        raise SystemExit("no watchable addresses: registry (verified, armed) and legacy both empty")
    wdb = sqlite3.connect(DB, timeout=30)  # wal-ok: detection writer commits each row; observations use get_readonly_connection
    wdb.executescript(SCHEMA)
    ensure_onchain_sells_table(wdb)
    ensure_detection_columns(wdb)
    wdb.close()
    global _ACTIVE_WRITER
    writer = _ACTIVE_WRITER = DetectionWriter(DB)
    confirmed = start_confirmed_feed(lambda: watch, writer, cache)
    j7_scanner = None if override_addrs else start_j7_hint_scanner(writer, cache)
    print(f"watching {len(watch)} wallets on-chain:", flush=True)
    for a, info in watch.items():
        print(f"  {info['handle']:<14} {a} [{info['status']}]", flush=True)
    t0 = time.time()
    backoff = fresh_backoff()
    disconnected_since = None
    ready_said = partial_said = False
    # Non-err frames received and those dropped before getTransaction, since process start.
    counts = {"frames": 0, "dropped_no_token_program": 0}
    try:
        if WS_URL is None:
            last_refresh, refresh = time.monotonic(), None
            while duration_s is None or time.time() - t0 < duration_s:
                mono = time.monotonic()
                if not override_addrs:
                    if refresh is None and mono - last_refresh >= REFRESH_S:
                        refresh = asyncio.get_running_loop().run_in_executor(None, desired_watchlist)
                    elif refresh is not None and refresh.done():
                        try:
                            got = refresh.result()
                        except Exception:
                            got = None
                        refresh, last_refresh = None, mono
                        if got is not None:
                            (watch, over_cap), refreshed_at = got, now_iso()
                wanted = tuple(sorted(watch))
                feeds = tuple(confirmed['status']['feeds'].values())
                healthy = [f for f in feeds if f.get('connected')
                    and tuple(f.get('acknowledged_owners', ())) == wanted
                    and f.get('head_slot') and f.get('head_monotonic') is not None
                    and 0 <= mono - f['head_monotonic'] <= 2.0]
                complete = bool(feeds) and len(healthy) == len(feeds)
                phase = confirmed['status'].get('handover', {}).get('phase', 'full')
                acked = len(watch) if complete else 0
                HEARTBEAT.write_text(json.dumps({
                    'pid': os.getpid(), 'ts': now_iso(), 'subs': acked, 'subs_desired': len(watch),
                    'subs_in_flight': 0 if complete else len(watch), 'subs_failed': 0,
                    'subs_over_cap': over_cap, 'refreshed_at': refreshed_at,
                    'connected': complete, 'provider': 'shyft',
                    'frames': sum(f.get('transactions', 0) for f in feeds),
                    'writer': dict(writer.stats), 'no_row': _no_row_stats(),
                    'j7_hint_scanner': j7_scanner.snapshot() if j7_scanner else {'enabled': False},
                    'shyft_confirmed': confirmed['status']}))
                if complete and ready_said != phase:
                    if _say_ready(primary=phase == 'primary'):
                        ready_said = phase
                await asyncio.sleep(.1)
            return
        while duration_s is None or time.time() - t0 < duration_s:
            try:
                async with websockets.connect(WS_URL, max_size=8_000_000, ping_interval=20) as ws:
                    backoff, disconnected_since = fresh_backoff(), None
                    subs = Subscriptions()
                    for req in subs.plan(watch):
                        await ws.send(json.dumps(req))
                    last_refresh, refresh = time.monotonic(), None
                    seen = set()

                    def beat():
                        # "subs" stays first and plain: onchain_armed_watchdog.sh reads it with sed. It
                        # counts ACKED subscriptions; the watchdog compares it with --desired-count.
                        HEARTBEAT.write_text(json.dumps({
                            "ts": now_iso(), "subs": len(subs.by_sub), **counts,
                            "writer": dict(writer.stats), "no_row": _no_row_stats(),
                            "shyft_confirmed": confirmed['status'],
                            "subs_desired": len(watch), "subs_in_flight": len(subs.requests),
                            "subs_failed": subs.failed, "subs_over_cap": over_cap,
                            "refreshed_at": refreshed_at, "connected": True}))
                    while duration_s is None or time.time() - t0 < duration_s:
                        if not override_addrs:
                            # Off the frame path: the read runs on an executor thread; this is one
                            # monotonic comparison per message.
                            if refresh is None and time.monotonic() - last_refresh >= REFRESH_S:
                                refresh = asyncio.get_running_loop().run_in_executor(
                                    None, desired_watchlist)
                            elif refresh is not None and refresh.done():
                                try:
                                    got = refresh.result()
                                except Exception:  # noqa: BLE001 - never reached; keep the set
                                    got = None
                                refresh, last_refresh = None, time.monotonic()
                                if got is not None:
                                    (watch, over_cap), refreshed_at = got, now_iso()
                                for req in subs.plan(watch):      # also re-asks a failed subscribe
                                    await ws.send(json.dumps(req))
                        try:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        except asyncio.TimeoutError:
                            beat()
                            continue
                        if subs.on_reply(msg):
                            if not ready_said and all(a in subs.addr_sub for a in watch):
                                ready_said = _say_ready()
                            elif not ready_said and not partial_said and subs.addr_sub:
                                partial_said = _say_ready("%d/%d" % (len(subs.addr_sub), len(watch)))
                            continue
                        if msg.get("method") != "logsNotification":
                            continue
                        frame_mono = time.monotonic()
                        val = msg["params"]["result"]["value"]
                        if val.get("err"):
                            continue
                        counts["frames"] += 1
                        if not frame_may_be_buy(val.get("logs")):
                            counts["dropped_no_token_program"] += 1
                            continue
                        sig = val["signature"]
                        if sig in seen:
                            continue
                        seen.add(sig)
                        if len(seen) > 5000:
                            seen = set(list(seen)[-2000:])
                        found = subs.lookup(msg["params"]["subscription"])
                        if found:
                            addr, info = found
                            proof_ready = ({'confirmed_seen': confirmed.get('has_receipt', confirmed['worker'].has_receipt)}
                                           if confirmed['worker'] else {})
                            asyncio.create_task(process_signature(sig, addr, info, writer, frame_mono,
                                sol_usd=cache.get("snapshot"), **proof_ready))
                        beat()
            except Exception as exc:
                delay = reconnect_delay(backoff, str(exc))
                print(f"ws reconnect after error: {exc}; sleep {delay:.0f}s", flush=True)
                if any(marker in str(exc) for marker in QUOTA_ERROR_MARKERS):
                    _rotate_key(str(exc)[:60])
                disconnected_since = disconnected_since or now_iso()
                # A DARK WATCHER SAYS SO. The beat above runs only inside the connected loop, so a
                # watcher backing off kept its last `subs` and read healthy to the watchdog. subs 0 and
                # connected false make both the J7 shadow gate (solana_watcher_healthy) and the
                # watchdog's drift rule see it; `ts` is fresh, so the age rule alone would not.
                try:
                    HEARTBEAT.write_text(json.dumps({
                        "ts": now_iso(), "subs": 0, **counts, "writer": dict(writer.stats), "no_row": _no_row_stats(),
                        "shyft_confirmed": confirmed['status'],
                        "subs_desired": len(watch),
                        "subs_in_flight": 0, "subs_failed": 0, "subs_over_cap": over_cap,
                        "refreshed_at": refreshed_at, "connected": False,
                        "disconnected_since": disconnected_since, "reconnect_in_s": delay}))
                except OSError as write_exc:
                    print(f"  heartbeat write failed: {write_exc}", flush=True)
                await asyncio.sleep(delay)
    finally:
        if j7_scanner is not None:
            await asyncio.to_thread(j7_scanner.close)
        if confirmed['stop'] is not None:
            confirmed['stop'].set()
        for task in confirmed['tasks']:
            task.cancel()
        await asyncio.gather(*confirmed['tasks'], return_exceptions=True)
        for worker in confirmed.get('workers', [confirmed['worker']]):
            if worker is not None:
                await asyncio.to_thread(worker.close)
        await asyncio.to_thread(writer.close)
        _stage_write({"ts": time.time(), "outcome": "writer_shutdown", "writer": dict(writer.stats)})


# THE WATCHER NAMES ITS OWN DEATH (2026-09-29, Beta, Alpha's order). A SIGTERM ended it with no
# line at all: on 2026-09-28/29 it died silently again and again; the killer (a test running the
# watchdog with a tmp STATE_ROOT, fixed there) had to be found from timestamps. SIGTERM and SIGINT
# are now blocked process-wide and taken by one thread, which prints the signal, the sender and the
# sender's parents from /proc, then exits 128+n exactly as the default action would, so watchdog and
# deploy kills keep working. It costs microseconds. SIGKILL (and an OOM kill) cannot be caught; the
# kernel log names those.
EXIT_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _proc_line(pid):
    """('<pid> [<cmdline>]', parent pid), or ('<pid> [gone]', None) once it has exited."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return f"{pid} [gone]", None
    return f"{pid} [{' '.join(cmd.split())[:160]}]", ppid    # one line: a `bash -c` script has newlines


def _sender_chain(pid, depth=3):
    if not pid:
        return "the kernel"
    parts = []
    while pid and len(parts) < depth:
        line, pid = _proc_line(pid)
        parts.append(line)
    return " < ".join(parts)


def _close_active_writer():
    """The TERM/INT half of run()'s finally: drain and close the writer, then record its final
    counters in the stage log and the watcher's own log (SOL-3)."""
    writer = _ACTIVE_WRITER
    if writer is None:
        return
    writer.close(drain_s=SHUTDOWN_DRAIN_S)
    _stage_write({"ts": time.time(), "outcome": "writer_shutdown", "signal": True,
                  "writer": dict(writer.stats)})
    print(f"{now_iso()} writer_shutdown {json.dumps(writer.stats, sort_keys=True)}", flush=True)


def log_exit_signals():
    """Call before any thread starts: a thread created earlier keeps the default action, and a
    signal delivered to it would still kill the process without the line."""
    signal.pthread_sigmask(signal.SIG_BLOCK, EXIT_SIGNALS)

    def wait():
        info = signal.sigwaitinfo(EXIT_SIGNALS)
        # THE EXIT IS UNCONDITIONAL (Gamma H1). With SIGTERM blocked in every thread, a log step
        # that raised (a full disk, a broken stdout) would end this thread and leave the process
        # unkillable by TERM: a deploy's stop would fail silently and two watchers would run. The
        # writer's drain (SOL-3) runs on its own thread and is waited for SHUTDOWN_DEADLINE_S at
        # most, so a blocked database cannot hold the exit either.
        try:
            try:
                print(f"{now_iso()} exit: {signal.Signals(info.si_signo).name} from "
                      f"{_sender_chain(info.si_pid)} uid {info.si_uid}", flush=True)
            except BaseException:       # noqa: BLE001 - nothing may stand between TERM and exit
                pass
            drain = threading.Thread(target=_close_active_writer, name="exit-drain", daemon=True)
            drain.start()
            drain.join(SHUTDOWN_DEADLINE_S)
        except BaseException:           # noqa: BLE001
            pass
        finally:
            os._exit(128 + info.si_signo)
    threading.Thread(target=wait, name="exit-signal", daemon=True).start()


def report():
    conn = get_readonly_connection(str(DB))
    conn.row_factory = None  # tuple rows, as before: the helper defaults to sqlite3.Row and Row does not slice
    try:
        rows = conn.execute(
            "SELECT handle, mint, sol_spent, detect_latency_s, feed_lag_vs_onchain_s, detected_at "
            "FROM onchain_wallet_detections ORDER BY id DESC LIMIT 30").fetchall()
    except sqlite3.OperationalError:
        print("no detections table yet")
        return
    print(f"{'handle':<14} {'mint':<12} {'sol':>7} {'chain_lat':>9} {'feed_vs_us':>11} when")
    wins = [r[4] for r in rows if r[4] is not None]
    for h, mint, sol, cl, fl, at in rows:
        fl_s = (f"+{fl:.1f}s" if fl and fl > 0 else (f"{fl:.1f}s" if fl is not None else "—"))
        print(f"{h:<14} {mint[:12]} {sol or 0:>7.3f} {cl or 0:>8.1f}s {fl_s:>11} {at[11:19]}")
    if wins:
        wins.sort()
        print(f"\nfeed-vs-onchain (n={len(wins)}): median {wins[len(wins)//2]:+.1f}s "
              f"(positive = on-chain detected FIRST)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=None, help="seconds (default: forever)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--addrs", help="comma-separated addresses to watch (verification mode)")
    ap.add_argument("--desired-count", action="store_true",
                    help="print how many addresses the watcher would subscribe now and exit "
                         "(onchain_armed_watchdog.sh's drift check); prints nothing on a failed read")
    ap.add_argument("--db", help="ledger path for --desired-count (default: the repo's)")
    args = ap.parse_args()
    if args.report:
        report()
        return
    if args.desired_count:
        global DB
        if args.db:
            DB = Path(args.db)
        from sniper.util.envfiles import load_env_files
        load_env_files(str(STATE_ROOT))             # the same cap the running watcher reads
        got = desired_watchlist()
        if got is None:
            raise SystemExit(1)
        print(len(got[0]))
        return
    override = args.addrs.split(",") if args.addrs else None
    log_exit_signals()
    try:
        asyncio.run(run(args.duration, override))
    except BaseException as exc:
        print(f"{now_iso()} exit: {type(exc).__name__}: {exc}", flush=True)
        raise
    print(f"{now_iso()} exit: run returned (--duration {args.duration})", flush=True)


if __name__ == "__main__":
    main()

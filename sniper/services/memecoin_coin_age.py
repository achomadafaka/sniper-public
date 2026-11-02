"""A coin's age from the MINT's own on-chain evidence, for the coin-age floor (2026-09-28, Alpha GO).

`coin_age_refusal` read `facts["pair_age_s"]`, the age of the OLDEST pair DexScreener lists. A coin
that lived on a launchpad curve and then graduated has one listed pair, the new pool, so the gate
read time-since-graduation: 2e32783b printed 54 s on a mint whose first transaction was 33,193 s
old (db8858ee 53 vs 4,424 s; 2304b5ca 61 vs 263 s; each a single Raydium pool). 3 of 14 audited
coin_too_young refusals flipped (docs/audits/2026-09-28-gate-instrument-audit.md).

ONLY ON THE REFUSAL BRANCH. `warm` runs in the entry hook, BEFORE the reservation transaction and
only when the pair age is under the floor, so an admitted coin pays nothing. `cached_age_s` is what
the gate reads inside the reservation: a dict lookup, never I/O (that transaction holds the ledger's
write lock).

EVIDENCE:
- Solana: the mint's first successful transaction, from Helius getTransactionsForAddress (ascending,
  signatures only) on the WATCHER key (ONCHAIN_WATCH_HELIUS_RPC_URL). One call, ~100 ms; research
  keys never feed a funded decision.
- EVM: our own volume tape's earliest tick on any pool of the mint. The tape keeps about an hour, so
  this is a LOWER bound, which is all a 120 s floor needs. An indexed read, no RPC.

It can only RAISE the age the gate reads, so it can only turn a refusal into an admission. Unknown,
a timeout, a 429 or a malformed answer leaves the gate exactly as it was. A found creation time is
immutable and cached for the life of the process; a miss is not cached.

Rollback: MEMECOIN_COIN_AGE_MINT_EVIDENCE=0.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

WAIT_S = 0.4
HTTP_TIMEOUT_S = 3.0
_LOCK = threading.Lock()
_FIRST_TS = {}                 # (chain, mint) -> epoch seconds of the mint's first evidence
_IN_FLIGHT = {}                # (chain, mint) -> threading.Event, one lookup per key at a time
_STATS = {"warmed": 0, "cache_hits": 0, "helius_calls": 0, "helius_ok": 0, "helius_429": 0,
          "helius_errors": 0, "wait_expired": 0, "tape_reads": 0, "tape_found": 0}


class LowerBoundEvidence(float):
    """Immutable one-sided evidence quality, retained with cached seconds."""
    __slots__ = ()


def enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get("MEMECOIN_COIN_AGE_MINT_EVIDENCE", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


def unknown_pair_check_enabled(env=None):
    """MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK (default 0): decide an UNKNOWN pair age from the mint
    just before signing (spec 2026-09-28-memecoin-unknown-pair-age-mint-evidence-design.md). It
    needs the mint evidence itself, so MEMECOIN_COIN_AGE_MINT_EVIDENCE=0 turns it off too."""
    env = os.environ if env is None else env
    return enabled(env) and str(env.get("MEMECOIN_COIN_AGE_UNKNOWN_PAIR_CHECK", "0")).strip().lower() in {
        "1", "true", "yes", "on"}


def stats():
    with _LOCK:
        return dict(_STATS, cached=len(_FIRST_TS))


def _count(key):
    with _LOCK:
        _STATS[key] += 1


def _key(network_id, mint):
    chain = str(network_id or "solana")
    return chain, (str(mint) if chain == "solana" else str(mint).lower())


def cached_age_s(network_id, mint, now=None):
    """Seconds since the mint's first evidence, from the cache only (never I/O), or None."""
    if not mint or not enabled():
        return None
    with _LOCK:
        first = _FIRST_TS.get(_key(network_id, mint))
    if first is None:
        return None
    age = max(0.0, (time.time() if now is None else now) - first)
    return LowerBoundEvidence(age) if isinstance(first, LowerBoundEvidence) else age


def _helius_first_ts(mint):
    from sniper.settings import memecoin as cfg
    shyft = cfg.MEMECOIN_SOLANA_INFRA_PROVIDER == "shyft"
    url = cfg.solana_read_rpc() if shyft else os.environ.get("ONCHAIN_WATCH_HELIUS_RPC_URL", "")
    if not url or (not shyft and ("helius" not in url or "api-key=" not in url)):
        return None
    body = {"jsonrpc": "2.0", "id": 1, "method": "getTransactionsForAddress",
            # CONFIRMED, not the finalized default (Lambda's review F1, 2026-09-28): finalized reads
            # ~10 s behind (live: default median 9.9 s, confirmed 1.1 s), so a mint younger than that
            # returned no rows and was ADMITTED -- the youngest coins, the ones the floor exists for.
            "params": [mint, {"transactionDetails": "signatures", "sortOrder": "asc", "limit": 5,
                              "commitment": "confirmed"}]}
    if shyft:
        # Standard RPC returns recent signatures: oldest successful timestamp in this bounded
        # sample is a lower bound on mint age, never a claim of full history.
        body = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [mint, {"limit": 5, "commitment": "confirmed"}]}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "curl/8.5.0"})
    _count("helius_calls")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            answer = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        _count("helius_429" if exc.code == 429 else "helius_errors")
        return None
    except Exception:                    # transport, timeout, malformed body: today's age stands
        _count("helius_errors")
        return None
    result = answer.get("result") if isinstance(answer, dict) else None
    rows = result if shyft and isinstance(result, list) else (result.get("data") if isinstance(result, dict) else None)
    if not isinstance(rows, list):
        rows = []
    if shyft:
        times = [row["blockTime"] for row in rows if isinstance(row, dict)
                 and row.get("err") is None and type(row.get("blockTime")) is int and row["blockTime"] > 0]
        _count("helius_ok" if times else "helius_errors")
        return LowerBoundEvidence(min(times)) if times else None
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        ts = row.get("blockTime")
        # A FAILED first transaction can name a precomputed mint before it exists; skip it.
        if row.get("err") is None and isinstance(ts, int) and ts > 0:
            _count("helius_ok")
            return float(ts)
    _count("helius_errors")
    return None


def _tape_first_ts(chain, mint, tape_path=None):
    """The earliest tick on any pool of the mint in our volume tape: a lower bound on its age."""
    from sniper.services.memecoin_volume_writer import db_path
    _count("tape_reads")
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % (tape_path or db_path()), uri=True, timeout=0.2)
        try:
            pools = [r[0] for r in conn.execute(
                "SELECT pool FROM onchain_pool_protocols WHERE chain_id=? AND mint=?", (chain, mint))]
            first = None
            for pool in pools:
                row = conn.execute("SELECT MIN(observed_at) FROM pool_ticks WHERE chain_id=? "
                                   "AND pool=?", (chain, pool)).fetchone()
                if row and row[0]:
                    first = row[0] if first is None else min(first, row[0])
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    try:
        ts = datetime.fromisoformat(str(first).replace("Z", "+00:00")).timestamp() if first else None
    except ValueError:
        return None
    if ts is not None and math.isfinite(ts):
        _count("tape_found")
        return ts
    return None


def warm(network_id, mint, *, wait_s=WAIT_S, lookup=None):
    """Resolve the mint's first evidence into the cache, waiting at most `wait_s`. Returns the
    cached age in seconds, or None. A lookup still running when the wait ends keeps running and
    fills the cache for the next attempt. Never raises."""
    if not mint or not enabled():
        return None
    key = _key(network_id, mint)
    with _LOCK:
        if key in _FIRST_TS:
            _STATS["cache_hits"] += 1
            first = _FIRST_TS[key]
            age = max(0.0, time.time() - first)
            return LowerBoundEvidence(age) if isinstance(first, LowerBoundEvidence) else age
        done = _IN_FLIGHT.get(key)
        start = done is None
        if start:
            done = _IN_FLIGHT[key] = threading.Event()
            _STATS["warmed"] += 1
    if start:
        def run():
            try:
                ts = (lookup(key[0], key[1]) if lookup is not None
                      else _helius_first_ts(key[1]) if key[0] == "solana"
                      else _tape_first_ts(key[0], key[1]))
                if ts is not None:
                    with _LOCK:
                        _FIRST_TS[key] = ts
            except Exception:
                logger.exception("coin age lookup failed for %s", key)
            finally:
                with _LOCK:
                    _IN_FLIGHT.pop(key, None)
                done.set()
        threading.Thread(target=run, daemon=True, name="coin-age-lookup").start()
    if not done.wait(max(0.0, float(wait_s))):
        _count("wait_expired")
    return cached_age_s(*key)

"""Seed the entry ladder's V4 PoolKey cache from our own swap tape. Reads only; quotes nothing.

THE GAP (2026-09-24). A Pons coin graduates, its new NATIVE-paired V4 pool starts trading within
seconds, and the ladder cannot quote that pool until V4 discovery learns its PoolKey from a
provider or an `eth_getLogs`. Measured on one retained hour of 4663: of the 34 keys the ladder
learned inside that hour, `verified_at` minus the pool's FIRST tape tick was p10 85 s, p50 346 s,
p90 417 s, max 577 s. PENIS was quoted at 06:46:23Z and 06:46:51Z and its busiest pool entered the
cache at 07:03:15Z; PENIS42069's landed at 06:55:02Z. In that window the only rung that answers is
relay, and on PENIS it quoted 2.90x the source price, which the chase cap then refused.

We already hold the answer. The volume writer sees the PoolManager `Initialize` event -- BEFORE the
pool's first swap -- decodes the full key and verifies it (`register_pool` raises when
`v4_pool_id(key)` does not equal the pool id). Since 2026-09-24 it also persists `fee`,
`tick_spacing` and `hooks`, so the key can be rebuilt from the tape exactly.

THE SEED IS EXACT, NEVER A HEURISTIC. Every candidate is rebuilt into a PoolKey and accepted only
when `v4_pool_id(key)` hashes back to the pool id the tape stored it under. A corrupted,
mis-attributed or partially-written row cannot enter the ladder's cache, because the hash IS the
pool's identity -- no currency matching, no "prefer the most ticks" tie-break, nothing that can be
subtly wrong. A row written before those columns existed carries NULLs and is SKIPPED: a guessed
fee hashes to a different pool, which is worse than being late.

WHERE IT RUNS: the runner's maintenance thread, on its own connection. The tape is opened READ-ONLY
(`file:...?mode=ro`) under `contextlib.closing`, because a bare `with` on a sqlite3 connection
commits the transaction and leaves the HANDLE OPEN. The volume worker is never given a handle to
the funded ledger.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3

logger = logging.getLogger(__name__)

# Bounded so one pass can never sweep the whole table, however far behind the cursor falls.
MAX_ROWS_PER_PASS = 200


def _tape_path(override=None):
    if override:
        return str(override)
    from sniper.services.memecoin_volume_writer import db_path
    return str(db_path())


def _key_from_row(row):
    """A PoolKey from a `pool_meta` row, or None when the row predates the key columns."""
    fee, spacing, hooks = row["fee"], row["tick_spacing"], row["hooks"]
    if fee is None or spacing is None:
        return None                      # written before 2026-09-24: skipped, never guessed
    try:
        return {"currency0": str(row["token0"]).lower(),
                "currency1": str(row["token1"]).lower(),
                "fee": int(fee), "tick_spacing": int(spacing),
                "hooks": str(hooks or "0x" + "00" * 20).lower()}
    except (TypeError, ValueError):
        return None


def seed_v4_pool_keys(conn, *, chain, since_iso, tape_path=None, limit=MAX_ROWS_PER_PASS,
                      progress=None):
    """Store every verifiable PoolKey the tape registered since `since_iso`. Returns the count.

    A caller-owned progress dict retains the last processed (time, pool), including tied times.
    Failed reads/writes leave it unchanged so a partial page is retried idempotently.
    """
    from sniper.execution.evm_v4_quote import V4
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_evm_v4_events import v4_pool_id

    conf = V4.get(str(chain))
    if not conf or not since_iso:
        return 0
    manager = conf["pool_manager"]
    seeded = 0
    try:
        with contextlib.closing(sqlite3.connect(
                "file:%s?mode=ro" % _tape_path(tape_path), uri=True, timeout=5,
                isolation_level=None)) as tape:
            tape.row_factory = sqlite3.Row
            # A TAPE THAT PREDATES THE KEY COLUMNS IS A DIFFERENT FACT FROM AN EMPTY PASS, and a
            # bare SELECT collapses both into 0: the missing column raises, the error is caught,
            # and the caller sees the same zero it sees on a quiet minute. Checked explicitly so
            # the log can say which one it was -- the writer adds these columns on its next start.
            have = {row[1] for row in tape.execute("PRAGMA table_info(pool_meta)")}
            missing = [c for c in ("fee", "tick_spacing", "hooks") if c not in have]
            if missing:
                logger.info("v4 key seed: tape pool_meta has no %s yet; nothing to seed until the "
                            "writer restarts", ", ".join(missing))
                return 0
            cursor = (progress or {}).get("cursor")
            after_time, after_pool = cursor if cursor else (str(since_iso), None)
            rows = tape.execute(
                "SELECT pool, token0, token1, resolved_at, fee, tick_spacing, hooks "
                "FROM pool_meta WHERE chain_id=? AND (resolved_at > ? "
                "OR (resolved_at = ? AND pool > ?)) "
                "ORDER BY resolved_at, pool LIMIT ?",
                (str(chain), after_time, after_time, after_pool,
                 min(MAX_ROWS_PER_PASS, max(0, int(limit))))).fetchall()
    except sqlite3.Error:
        logger.info("v4 key seed: tape unavailable", exc_info=True)
        return 0
    for row in rows:
        pool = str(row["pool"] or "").lower()
        # A V4 pool id is the 32-byte hash. The same table also holds V3 pools (20-byte addresses)
        # and, on 4663, bonding-curve contracts -- 1,206 of 2,630 ticked rows in the sample hour.
        if not (pool.startswith("0x") and len(pool) == 66):
            continue
        key = _key_from_row(row)
        if key is None:
            continue
        try:
            if v4_pool_id(key).lower() != pool:
                continue                 # the row does not describe the pool it is filed under
        except ValueError:
            continue
        try:
            m.put_evm_v4_pool_key(conn, chain_id=str(chain), pool_id=pool,
                                  pool_manager=manager, key=key)
            seeded += 1
        except Exception:
            logger.exception("v4 key seed: could not store %s", pool)
            return seeded
    if progress is not None and rows:
        progress["cursor"] = (rows[-1]["resolved_at"], rows[-1]["pool"])
    return seeded

"""Live display marks for held Robinhood (4663) coins, read off our own swap tape. Display only.

Operator order 2026-09-28 (relayed by Gamma): the 4663 "Market price" and the position chart used
the DexScreener mark, which is written about every 15 s and lags. On UNIPCS (position 66359) the
exact route read -29.1% at 09:42:58Z while DexScreener read -12.1% at 09:43:09Z and -23.1% at
09:43:24Z. Our tape (data/memecoin_onchain_volume.db pool_ticks) holds every swap at sequencer-feed
speed: tape minus feed p50 -0.031 s, n=4529 (scripts/research/robinhood_feed_vs_tape.py).

THE PRICE IS A RATIO ANCHORED ON OUR OWN ENTRY, the method memecoin_evm_post_close_marks uses:
ratio_t / ratio_anchor x our entry fill price. The unknown 10**(base - quote) decimals factor
cancels, so no token decimals are needed. The anchor is the pool state our entry transaction left
(same pool, same transaction). Two named biases follow:
- At the anchor the mark equals our AVERAGE fill, which sits below the post-swap spot by about half
  of our own price impact, and that offset carries into every later mark.
- The quote token's own USD move (WETH over the hold) is not measured.
On position 66384 the mark ran about 6 points above the exact whole-balance route quote through the
hold, with the same shape: the route quote carries our sell impact and fees, the spot does not.

UNKNOWN IS ABSENT, NEVER ZERO. No pool binding, no quote side, no entry fill or no tick of it in the
tape (the tape keeps about an hour; a found anchor is cached for the life of the process), no
sqrt price (Pons v2 curves and V2 pools carry none), an unreadable ratio, or an unhealthy feed all
yield no entry, and the ticker shows what it showed before.

A HEALTHY FEED is: the writer's own last delivery (volume_feed.received) within FEED_TTL_S, the
pool's subscription lag (volume_feed_lag, V4 for a 32-byte pool id, V3 otherwise) measured within
LAG_TTL_S and no more than LAG_MAX_S, and no unresolved tick gap on the chain. A quiet pool keeps its
last swap's price, because a concentrated-liquidity spot price moves only on a swap; after
CARRY_AFTER_S it is flagged `carried`. Ticks count only when their block hash matches the canonical
header in block_times (orphaned forks drop out).

Nothing here feeds an exit, a rail, an order or the mark tape. Rollback: MEMECOIN_EVM_TAPE_DISPLAY=0
and a web restart.
"""
from __future__ import annotations

import contextlib
import logging
import math
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SOURCE = "onchain_tape_entry_anchor"
CHAIN = "4663"
FEED_TTL_S = 5.0
LAG_TTL_S = 120.0
LAG_MAX_S = 5.0
CARRY_AFTER_S = 5.0
MAX_POINTS = 600
SERIES_SCAN = 20000    # rows read for one chart; about 100 min of the busiest pool
# position id -> (pool, fill price, anchor ratio, anchor block, anchor log index, meta). A found
# anchor never changes; a miss is not cached and is asked again on the next read.
_ANCHORS = {}

_VERIFIED = (
    "SELECT t.observed_at, t.sqrt_price_x96, t.block_no, t.log_index, t.liquidity FROM pool_ticks t "
    "JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
    "AND lower(b.block_hash)=lower(t.block_hash) "
    "WHERE t.chain_id=? AND t.pool=? AND t.sqrt_price_x96 IS NOT NULL "
    "AND t.log_index IS NOT NULL AND t.block_hash IS NOT NULL AND t.block_hash!='' ")


def enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get("MEMECOIN_EVM_TAPE_DISPLAY", "1")).strip().lower() not in {
        "0", "false", "no", "off"}


def _ro(path):
    # The web ticker runs four times a second per open tab: never wait long on a lock.
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=0.2, isolation_level=None)


def _paths(tape_path, bind_path):
    from sniper.services.memecoin_evm_post_close_marks import _bind_path, _tape_path
    return _tape_path(tape_path), _bind_path(bind_path)


def _ts(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _ratio(sqrt_price_x96, meta):
    from sniper.services.memecoin_evm_post_close_marks import _ratio as ratio
    value = ratio(sqrt_price_x96, meta)
    return value if value and math.isfinite(value) and value > 0 else None


def _anchor(conn, bind, tape, pid):
    hit = _ANCHORS.get(pid)
    if hit is not None:
        return hit
    row = bind.execute("SELECT pool FROM evm_position_pools WHERE position_id=? AND chain_id=?",
                       (pid, CHAIN)).fetchone()
    if not row or not row[0]:
        return None
    pool = str(row[0])
    meta = tape.execute("SELECT quote_side, token0_decimals, token1_decimals FROM pool_meta "
                        "WHERE chain_id=? AND pool=?", (CHAIN, pool)).fetchone()
    if not meta or meta[0] is None:
        return None
    meta = {"quote_side": meta[0], "token0_decimals": meta[1], "token1_decimals": meta[2]}
    fill = conn.execute("SELECT price, tx_sig FROM memecoin_fills WHERE position_id=? "
                        "AND kind='entry' AND price IS NOT NULL AND tx_sig IS NOT NULL "
                        "ORDER BY id LIMIT 1", (pid,)).fetchone()
    try:
        price = float(fill[0]) if fill else None
    except (TypeError, ValueError):
        price = None
    if price is None or not math.isfinite(price) or price <= 0:
        return None
    tick = tape.execute(_VERIFIED + "AND t.tx_sig=? ORDER BY t.block_no DESC, t.log_index DESC "
                        "LIMIT 1", (CHAIN, pool, str(fill[1]))).fetchone()
    ratio = _ratio(tick[1], meta) if tick else None
    if ratio is None:
        return None
    hit = (pool, price, ratio, int(tick[2]), int(tick[3]), meta)
    _ANCHORS[pid] = hit
    return hit


def _price(anchor, sqrt_price_x96):
    ratio = _ratio(sqrt_price_x96, anchor[5])
    if ratio is None:
        return None
    price = anchor[1] * ratio / anchor[2]
    return price if math.isfinite(price) and price > 0 else None


def _health(tape, now_ts, chain=CHAIN):
    feed = tape.execute("SELECT received FROM volume_feed WHERE chain_id=?", (chain,)).fetchone()
    lag = tape.execute("SELECT v4_lag_s, v3_lag_s, observed FROM volume_feed_lag WHERE chain_id=?",
                       (chain,)).fetchone()
    try:
        feed_age = now_ts - float(feed[0])
        lag_age = now_ts - float(lag[2])
    except (TypeError, ValueError):
        return None                       # no row or a NULL stamp: unknown, never healthy
    # -1 s: `now_ts` is the real clock and the writer stamps the host clock (about 0.05 s apart).
    if not -1.0 <= feed_age <= FEED_TTL_S or not -1.0 <= lag_age <= LAG_TTL_S:
        return None
    if tape.execute("SELECT 1 FROM tick_gaps WHERE chain_id=? AND resolved_at IS NULL LIMIT 1",
                    (chain,)).fetchone():
        return None
    checked = datetime.fromtimestamp(float(feed[0]), timezone.utc).isoformat()
    return {"checked_at": checked.replace("+00:00", "Z"), "v4": lag[0], "v3": lag[1]}


def _mark(conn, bind, tape, pid, health, now_ts, *, liquidity=False):
    anchor = _anchor(conn, bind, tape, pid)
    if anchor is None:
        return None
    lag = health["v4" if len(anchor[0]) == 66 else "v3"]
    if lag is None or float(lag) > LAG_MAX_S:
        return None
    latest = tape.execute(
        _VERIFIED + "AND (t.block_no>? OR (t.block_no=? AND t.log_index>=?)) "
        "ORDER BY t.block_no DESC, t.log_index DESC LIMIT 1",
        (CHAIN, anchor[0], anchor[3], anchor[3], anchor[4])).fetchone()
    price = _price(anchor, latest[1]) if latest else None
    seen = _ts(latest[0]) if latest else None
    if price is None or seen is None:
        return None
    out = {"price": price, "observed_at": latest[0], "source": SOURCE,
           "checked_at": health["checked_at"], "carried": now_ts - seen > CARRY_AFTER_S}
    if liquidity:
        out["liquidity"] = latest[4]
    return out


_SOURCE_TICKS = (
    "SELECT t.pool, t.sqrt_price_x96, t.block_no, t.log_index FROM pool_ticks t "
    "JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
    "AND lower(b.block_hash)=lower(t.block_hash) "
    "WHERE t.chain_id=? AND t.tx_sig=? AND t.sqrt_price_x96 IS NOT NULL "
    "AND t.log_index IS NOT NULL AND t.block_hash IS NOT NULL AND t.block_hash!='' ")


def source_move(chain, mint, source_tx, now_ts, *, tape_path=None):
    """How far the COIN moved since the source's swap, from our own tape (spec
    2026-10-08-memecoin-evm-entry-gates-onchain-design.md §3 E3). Read-only; never raises.

    {move_pct, why, pool, source_block, latest_block}: move_pct = 100 x (coin price after the newest
    verified swap on the source's pool / coin price right after the source's LAST swap on it - 1).
    Both are post-swap sqrtPrice spots, oriented by pool_meta.quote_side (`_ratio`), so a coin that
    is token1 reads 1/P. A ratio: decimals cancel and no USD is used. Unknown (move_pct None) when the
    store is absent, the feed or the protocol stream is unhealthy, the tape holds no verified
    sqrtPrice tick of the source transaction on any of the coin's pools (V2 pools carry none), or
    the source touched two of them. Seen and nothing later is a measured 0.0."""
    out = {"move_pct": None, "why": None, "pool": None, "source_block": None, "latest_block": None}
    from sniper.services.memecoin_evm_post_close_marks import _tape_path
    path = _tape_path(tape_path)
    if not os.path.exists(path):
        return {**out, "why": "tape_absent"}
    mint, chain = str(mint or "").lower(), str(chain)
    try:
        with contextlib.closing(_ro(path)) as tape:
            health = _health(tape, float(now_ts), chain=chain)
            if health is None:
                return {**out, "why": "tape_unhealthy"}
            metas = {str(r[0]): {"quote_side": r[1], "token0_decimals": r[2],
                                 "token1_decimals": r[3]}
                     for r in tape.execute(
                         # Two indexed lookups: `token0=? OR token1=?` makes the planner scan every
                         # pool of the chain (134 ms measured on 4663).
                         "SELECT pool, quote_side, token0_decimals, token1_decimals FROM pool_meta "
                         "WHERE chain_id=? AND token0=? AND quote_side IS NOT NULL UNION ALL "
                         "SELECT pool, quote_side, token0_decimals, token1_decimals FROM pool_meta "
                         "WHERE chain_id=? AND token1=? AND quote_side IS NOT NULL",
                         (chain, mint, chain, mint))}
            ticks = [r for r in tape.execute(_SOURCE_TICKS, (chain, str(source_tx)))
                     if str(r[0]) in metas]
            if not ticks:
                return {**out, "why": "source_unseen"}
            if len({str(r[0]) for r in ticks}) > 1:
                return {**out, "why": "multi_pool_source"}
            base = max(ticks, key=lambda r: (int(r[2]), int(r[3])))
            pool, meta = str(base[0]), metas[str(base[0])]
            lag = health["v4" if len(pool) == 66 else "v3"]
            if lag is None or float(lag) > LAG_MAX_S:
                return {**out, "why": "tape_unhealthy", "pool": pool}
            latest = tape.execute(
                _VERIFIED + "AND (t.block_no>? OR (t.block_no=? AND t.log_index>=?)) "
                "ORDER BY t.block_no DESC, t.log_index DESC LIMIT 1",
                (chain, pool, base[2], base[2], base[3])).fetchone()
            r0, r1 = _ratio(base[1], meta), _ratio(latest[1], meta) if latest else None
    except (sqlite3.Error, OSError, TypeError, ValueError):
        logger.exception("source move read failed chain=%s mint=%s", chain, mint)
        return {**out, "why": "tape_error"}
    if r0 is None or r1 is None:
        return {**out, "why": "unpriceable", "pool": pool}
    return {"move_pct": (r1 / r0 - 1.0) * 100.0, "why": "ok", "pool": pool,
            "source_block": int(base[2]), "latest_block": int(latest[2])}


def exit_mark(conn, position_id, now_ts, *, tape_path=None, bind_path=None):
    """The exit dust guard's own 4663 market mark (memecoin_exit_dust_guard), or None.

    The same mark as tape_marks, with two differences. MEMECOIN_EVM_TAPE_DISPLAY does not switch it
    off: a display switch must never change an exit. And the newest tick must have printed in-range
    liquidity above zero: a pulled pool's last price is not a market (Alpha 2026-10-06). A quiet pool
    that was pulled without a later swap still reads its last price; the guard then holds and pages.
    Never raises; every unknown is None."""
    tape_path, bind_path = _paths(tape_path, bind_path)
    if not (os.path.exists(tape_path) and os.path.exists(bind_path)):
        return None                       # no store on this host: no mark, without a traceback
    try:
        with contextlib.closing(_ro(bind_path)) as bind, \
                contextlib.closing(_ro(tape_path)) as tape:
            health = _health(tape, float(now_ts))
            if health is None:
                return None
            mark = _mark(conn, bind, tape, int(position_id), health, float(now_ts), liquidity=True)
    except (sqlite3.Error, OSError, TypeError, ValueError):
        logger.exception("4663 exit tape mark failed for position %s", position_id)
        return None
    try:
        if mark is None or int(mark.pop("liquidity")) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    return mark


def tape_marks(conn, positions, now_ts, *, tape_path=None, bind_path=None, env=None):
    """{position id: {price, observed_at, source, checked_at, carried}} for held 4663 positions.
    Never raises: a missing, locked or unhealthy store is {}."""
    held = {int(p["id"]) for p in positions or ()
            if str(p.get("network_id")) == CHAIN and p.get("id") is not None}
    if not held or not enabled(env):
        return {}
    tape_path, bind_path = _paths(tape_path, bind_path)
    out = {}
    try:
        with contextlib.closing(_ro(bind_path)) as bind, \
                contextlib.closing(_ro(tape_path)) as tape:
            health = _health(tape, float(now_ts))
            if health is None:
                return {}
            for pid in held:
                try:
                    mark = _mark(conn, bind, tape, pid, health, float(now_ts))
                except (sqlite3.Error, TypeError, ValueError):
                    logger.exception("4663 tape mark failed for position %s", pid)
                    continue
                if mark is not None:
                    out[pid] = mark
    except (sqlite3.Error, OSError):
        return {}
    # Closed: the ticker never reads its anchor again. list() copies the keys in one step, so a
    # request thread and the SSE thread pruning together cannot raise mid-iteration.
    for pid in list(_ANCHORS):
        if pid not in held:
            _ANCHORS.pop(pid, None)
    return out


def tape_series(conn, position_id, *, max_points=MAX_POINTS, tape_path=None, bind_path=None,
                env=None):
    """The position's chart points from its entry on: [{t, mid, mcap: None, source}], oldest
    first. A busy pool prints ~200 swaps a minute, so the whole hold (up to SERIES_SCAN rows) is
    thinned to about `max_points` by an even stride that always keeps the newest swap.
    [] when anything is unknown."""
    if not enabled(env):
        return []
    tape_path, bind_path = _paths(tape_path, bind_path)
    try:
        with contextlib.closing(_ro(bind_path)) as bind, \
                contextlib.closing(_ro(tape_path)) as tape:
            anchor = _anchor(conn, bind, tape, int(position_id))
            if anchor is None:
                return []
            rows = tape.execute(
                _VERIFIED + "AND (t.block_no>? OR (t.block_no=? AND t.log_index>=?)) "
                "ORDER BY t.block_no DESC, t.log_index DESC LIMIT ?",
                (CHAIN, anchor[0], anchor[3], anchor[3], anchor[4], SERIES_SCAN)).fetchall()
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return []
    step = max(1, math.ceil(len(rows) / max(1, int(max_points))))
    points = []
    for row in reversed(rows[::step]):
        price = _price(anchor, row[1])
        if price is not None:
            points.append({"t": row[0], "mid": price, "mcap": None, "source": SOURCE})
    return points

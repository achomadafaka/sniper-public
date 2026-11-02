"""The minutes after a funded EVM stop, read off the on-chain swap tape. Observation only.

WHY THIS EXISTS (2026-09-23, operator: keep reading every entered token for at least 90 s after the
close). `77bc333b` made post-close marks a Solana guarantee: `open_swap_mark_positions` feeds the
Solana swap-mark lane's vault book, so a closed Solana row keeps printing. A closed 4663/8453 row is
listed there too and gets NOTHING, because EVM marks come from the exact balance-based exit preview
in `refresh_open_positions`, and a preview after the balance was sold prices nothing real
(`c968d878` says so in `open_swap_mark_positions`'s docstring; this module is the other half).

⚠️ THE SERIES IS A RATIO ANCHORED ON OUR OWN EXIT, NOT A PRICE DERIVED FROM DECIMALS. Measured on
2026-09-23 over the 179 distinct pools bound to funded positions: 165 are in `pool_meta`, 163 carry
a `quote_side`, and both token decimals resolve for SIX (one from `pool_meta` alone, six after
joining `volume_token_decimals`). Without the base decimals
`memecoin_refusal_shadow.sqrt_price_to_quote_per_token` returns an ATOMIC RATIO, and an atomic ratio
must never be written into `memecoin_position_marks.mid`, which the app and the replay read as a USD
price. The unknown 10**(base - quote) factor CANCELS in `ratio_t / ratio_anchor`, so multiplying
that pure ratio by the position's own realized exit price gives a USD-scaled observation on every pool with
a known quote side — 163 of 179 rather than 6.

⚠️ THE ANCHOR IS A POST-SWAP STATE AND THE EXIT PRICE IS AN AVERAGE FILL. At the
anchor, ratio_t / ratio_anchor is one, so this series equals the realized exit price by
construction. That does not make it the pool's post-sale spot price: the average fill includes
prices traversed by our own sale. The scale bias persists in later observations. Quote-token
USD movement is also not measured by this ratio. SOURCE names the post-swap anchor so these
modeled observations cannot be confused with executable sell quotes.

BOUNDS AND ABSENCES. The population is `recently_closed_real_positions`, already capped at the
newest 2 * MEMECOIN_MAX_CONCURRENT: up to 14 positions at the default, each requiring multiple
store reads. Everything
unknown is ABSENT, never zero, because a 0.0 here would read as a measured free coin: no pool
binding, no `pool_meta` row, no `quote_side`, no anchor tick, no later tick, an unparseable
`sqrt_price_x96`, or a missing exit fill price all yield no entry at all. A matching canonical block hash and
block/log order are required; source time is block time, never delayed receipt time.

CHAIN 8453 YIELDS NOTHING TODAY, by population rather than by defect: `evm_position_pools` holds 191
bindings and every one is chain 4663. Solana keeps its own lane and is not read here.

NEVER A DECISION. These rows carry their own `source`, so `write_marks`'s per-class dedupe cannot
let them silence the pool-mid or fast-lane series (or be silenced by it), and every exit decision
reads `is_open=1` positions and the live price map, never this tape.
"""
from __future__ import annotations

import contextlib
import logging
import math
import sqlite3

logger = logging.getLogger(__name__)

# The bias is IN THE NAME: the anchor is the pool's state after our own sale moved it.
SOURCE = "onchain_tape_post_close_post_swap_anchor"


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5,
                           isolation_level=None)


def _tape_path(override=None):
    if override:
        return str(override)
    from sniper.services.memecoin_volume_writer import db_path
    return str(db_path())


def _bind_path(override=None):
    if override:
        return str(override)
    from sniper.persistence.db_ticks import ticks_db_path
    return str(ticks_db_path())


def _exit_fill(conn, position_id, close_sig):
    """(price_usd, sig) for the transaction that closed this position, or (None, None)."""
    try:
        signature_filter = " AND tx_sig=?" if close_sig else ""
        params = (int(position_id), str(close_sig)) if close_sig else (int(position_id),)
        row = conn.execute(
            "SELECT price, tx_sig FROM memecoin_fills WHERE position_id=? AND kind!='entry' "
            "AND price IS NOT NULL" + signature_filter + " ORDER BY id DESC LIMIT 1",
            params).fetchone()
    except sqlite3.Error:
        return None, None
    if not row:
        return None, None
    price = row[0]
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(price) and price > 0):
        return None, None
    return price, (row[1] or close_sig)


def _ratio(sqrt_price_x96, meta):
    """The pool's price as an atomic ratio, or None. Units cancel against the anchor."""
    from sniper.services.memecoin_refusal_shadow import sqrt_price_to_quote_per_token
    return sqrt_price_to_quote_per_token(
        sqrt_price_x96, quote_side=meta["quote_side"],
        token0_decimals=meta["token0_decimals"], token1_decimals=meta["token1_decimals"])


def evm_post_close_marks(conn, *, now_iso, window_s=None, tape_path=None, bind_path=None):
    """`{(chain_id, mint): {price, observed_at, source}}` for recently closed funded EVM positions.

    Read-only on both stores, and never raises into the caller: the fast lane's pass owns a funded
    price map and one bad position must not deny the tape to the others.
    """
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    if window_s is None:
        window_s = float(getattr(cfg, "MEMECOIN_POST_CLOSE_MARK_S", 0.0) or 0.0)
    if window_s <= 0:
        return {}
    try:
        closed = [p for p in m.recently_closed_real_positions(
            conn, within_s=window_s, now_iso=now_iso)
            if str(p.get("network_id") or "") in ("4663", "8453")]
    except Exception:
        logger.exception("post-close marks: closed-position read failed")
        return {}
    if not closed:
        return {}
    out = {}
    try:
        # TWO STORES, AND THEY ARE NOT THE SAME FILE: the binding lives in the ticks DB and the
        # swap tape in the volume DB. `contextlib.closing`, never a bare `with` -- a `with` on a
        # sqlite3 connection commits the transaction and leaves the HANDLE OPEN.
        with contextlib.closing(_ro(_bind_path(bind_path))) as bind, \
                contextlib.closing(_ro(_tape_path(tape_path))) as tape:
            for pos in closed:
                key = (str(pos.get("network_id")), pos.get("mint"))
                if key in out:
                    continue  # Newest usable close wins; never replace it with an older bag.
                try:
                    entry = _position_mark(conn, bind, tape, pos, window_s, now_iso)
                except Exception:
                    logger.exception("post-close mark failed for position %s", pos.get("id"))
                    continue
                if entry is not None:
                    out[key] = entry
    except sqlite3.Error:
        # A missing or locked store is an absent observation, not an error the fast lane must wear.
        logger.info("post-close marks: tape unavailable", exc_info=True)
        return {}
    return out


def _position_mark(conn, bind, tape, pos, window_s, now_iso):
    import datetime as dt

    chain = str(pos.get("network_id") or "")
    closed_at = pos.get("closed_at")
    if not closed_at:
        return None
    row = bind.execute("SELECT pool FROM evm_position_pools WHERE position_id=? AND chain_id=?",
                       (int(pos["id"]), chain)).fetchone()
    if not row or not row[0]:
        return None                                   # no binding: absent, not zero
    pool = str(row[0])
    meta = tape.execute(
        "SELECT quote_side, token0_decimals, token1_decimals FROM pool_meta "
        "WHERE chain_id=? AND pool=?", (chain, pool)).fetchone()
    if not meta or meta[0] is None:
        return None                                   # unknown quote side: the ratio has no side
    meta = {"quote_side": meta[0], "token0_decimals": meta[1], "token1_decimals": meta[2]}
    price, close_sig = _exit_fill(conn, pos["id"], pos.get("close_sig"))
    if price is None:
        return None
    try:
        close_dt = dt.datetime.fromisoformat(str(closed_at).replace('Z', '+00:00'))
        now_dt = dt.datetime.fromisoformat(str(now_iso).replace('Z', '+00:00'))
        if close_dt.tzinfo is None or now_dt.tzinfo is None:
            return None
        close_dt = close_dt.astimezone(dt.timezone.utc)
        end_iso = min(close_dt + dt.timedelta(seconds=float(window_s)),
                      now_dt.astimezone(dt.timezone.utc)).isoformat()
    except (TypeError, ValueError):
        return None                                   # an unparseable close is not a window
    # Chain identity and order are required: insertion/receipt order includes delayed pre-close
    # logs and orphaned forks. A missing header/hash is unavailable evidence, not a fresh mark.
    verified = (
        "SELECT b.block_ts, t.sqrt_price_x96, t.block_no, t.log_index "
        "FROM pool_ticks t JOIN block_times b "
        "ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
        "AND lower(b.block_hash)=lower(t.block_hash) "
        "WHERE t.chain_id=? AND t.pool=? AND t.sqrt_price_x96 IS NOT NULL "
        "AND t.log_index IS NOT NULL AND t.block_hash IS NOT NULL "
        "AND t.block_hash!='' AND b.block_ts<=? "
    )
    # THE ANCHOR IS OUR OWN EXIT TRANSACTION when the tape holds it: same block, same swap, so the
    # ratio the series divides by is the state our sale itself produced. Only when that row is
    # absent (the tape retains about an hour, and a reset can drop a span) does the first tick at
    # or after the close stand in.
    anchor = None
    if close_sig:
        anchor = tape.execute(
            verified + "AND t.tx_sig=? ORDER BY t.block_no DESC, t.log_index DESC LIMIT 1",
            (chain, pool, end_iso, str(close_sig))).fetchone()
    if anchor is None:
        anchor = tape.execute(
            verified + "AND b.block_ts>=? ORDER BY t.block_no, t.log_index LIMIT 1",
            (chain, pool, end_iso, close_dt.isoformat())).fetchone()
    if anchor is None:
        return None
    latest = tape.execute(
        verified + "AND (t.block_no, t.log_index)>=(?,?) "
        "ORDER BY t.block_no DESC, t.log_index DESC LIMIT 1",
        (chain, pool, end_iso, anchor[2], anchor[3])).fetchone()
    if latest is None:
        return None
    anchor_ratio, latest_ratio = _ratio(anchor[1], meta), _ratio(latest[1], meta)
    if not anchor_ratio or not latest_ratio or anchor_ratio <= 0 or latest_ratio <= 0:
        return None                                   # unreadable state: absent, never 0.0
    marked_price = price * (latest_ratio / anchor_ratio)
    if not math.isfinite(marked_price):
        return None
    return {"price": marked_price,
            "observed_at": latest[0],
            "source": SOURCE}

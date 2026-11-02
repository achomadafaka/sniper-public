"""PumpSwap tape reader for the desk's charts (operator order (a), 2026-10-23): the same swap rows the curve readers
produce, from the universe worker's PumpSwap capture (data/desk/pumpswap/tape.db), so JUDGE's structure block, RISK's bars
and the attention signals keep seeing a coin after it graduates, and see a PumpSwap candidate at all.

Read-only (mode=ro), zero network, zero provider calls, never on the fill path. Only the coin's CANONICAL pool is read
(PDA ["pool", 0, PDA["pool-authority", mint], mint, wSOL]; accounts_for), never a mint-wide search, and only wSOL-quoted rows.

PRICE. SOL per whole token after the swap = (post_quote + virtual_quote) / 1e9 / (post_base / 1e6), the curve candles' unit.
virtual_quote is a PROVEN value: a legacy buy event's own virtual_quote_atomic with source VQ_SOURCE (the as-of pricer's rule).
It is NOT constant per pool (2DCcA4Nh's pool, 2026-10-23: 17.5634 -> 17.5625 SOL between events; older pools read 0), so each
swap uses the most recent proof at or before it in chain order (a proof received before the window counts). Between proofs it
drifts by under 0.01% per swap (ponytail: carried forward, not interpolated; exact needs v2/sell events to carry it). No proof
yet = price None (the flow still counts): unproven is never zero. A SELL event has no proven post state (the decoder leaves it NULL); its price is the NEXT swap's pre-trade reserves
in chain order (slot, tx_index, ordinal), which is the pool right after the sell; the window's last sell stays unpriced. A row with a null required field raises TapeUnreadable, never a silent skip.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_left
import sqlite3
from pathlib import Path

from sniper.services.memecoin_desk_universe import PS_ARCHIVE_AFTER_S

WSOL_MINT = "So11111111111111111111111111111111111111112"
VQ_SOURCE = "pumpswap_grpc:buy_event_virtual_quote"
PROOF_EVENTS = ("buy", "buy_exact_quote_in")
# gaps_ps causes that are per-transaction decode losses from ANY pool (~0.6 rows/s), not capture discontinuities
# (scripts/research/pumpswap_census_hourly.DECODE_CAUSES). A lost swap of THIS pool shows in its own rows instead.
DECODE_CAUSES = ("decode_loss", "virtual_quote_unproven")
GRADUATION_REAL_SOL = 84_000_000_000     # a classic curve completes near 85 SOL real; the last print at/above = graduated


# New cohorts pass explicit validated decimals (None keeps flow, price unknown), strict slot cuts and integrity=True.
# Legacy callers retain six-decimal pricing and their frozen loss interpretation.


class TapeUnreadable(Exception):
    pass


def tape_path(desk_dir):
    return Path(desk_dir) / "pumpswap" / "tape.db"


def canonical_pool(mint):
    from sniper.execution.solana_native_entry_quote import accounts_for
    return accounts_for(mint)["pool"]


def _ro(desk_dir):
    p = tape_path(desk_dir)
    if not p.exists():
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=1)
    except sqlite3.Error as exc:
        raise TapeUnreadable("ps_open:%s" % type(exc).__name__) from None


def _proof(payload):
    """The event's own proven virtual quote (int) or None (the as-of pricer's rule: a legacy buy, its source, atomic ==
    str(reserves), u64)."""
    try:
        p = json.loads(payload or "{}")
        raw = p.get("virtual_quote_atomic")
        if (p.get("virtual_quote_source") != VQ_SOURCE or p.get("ix_name") not in PROOF_EVENTS
                or not isinstance(raw, str) or str(p.get("virtual_quote_reserves")) != raw):
            return None
        v = int(raw)
    except (ValueError, TypeError):
        return None
    return v if 0 <= v < 2**64 else None


def retained_from(conn, pool, before):
    """The approved seed/tip lower receipt bound; unavailable or malformed metadata is unknown."""
    try:
        row = conn.execute("SELECT keep_from FROM ps_keep WHERE pool=?", (pool,)).fetchone()
    except sqlite3.OperationalError as exc:
        if str(exc) != "no such table: ps_keep":
            raise TapeUnreadable("ps_window:keep_unreadable") from None
        row = None  # Older tape schemas have no keep window; use the explicit archive-age bound.
    except sqlite3.Error:
        raise TapeUnreadable("ps_window:keep_unreadable") from None
    floor = row[0] if row else before - PS_ARCHIVE_AFTER_S
    if type(floor) not in (int, float) or not math.isfinite(floor) or floor < 0:
        raise TapeUnreadable("ps_window:keep_unknown")
    return floor


def window_rows(conn, columns, pool, lo, hi, *, cut_slot=None, limit=None,
                order="slot,COALESCE(tx_index,-1),ordinal", params=(), predicate="", predicate_params=()):
    """One receipt-index range establishes exact slot bounds; one bounded pool-slot walk returns rows.

    SQL projection/order/predicate are trusted module constants, not external input. The LEFT JOIN
    keeps the invalid-slot witness even when MIN/MAX finds no usable row: NULL is never quiet.
    """
    if (any(type(t) not in (int, float) or not math.isfinite(t) for t in (lo, hi))
            or (cut_slot is not None and (type(cut_slot) is not int or cut_slot < 0))):
        raise TapeUnreadable("ps_window:bounds_unknown")
    if hi <= lo:
        return []
    try:
        # swaps_ps_pool_received bounds one pool's receipts; swaps_ps_received alone walks every pool's rows in the
        # window (6 h = 3.5 s on the live tape). A tape built before the index keeps the old path; a wrong shape fails.
        by_pool = tuple(r[2] for r in conn.execute("PRAGMA index_info(swaps_ps_pool_received)"))
        if by_pool and by_pool[:2] != ("pool", "received_at"):
            raise TapeUnreadable("ps_window:index_unknown")
        received_index = "swaps_ps_pool_received" if by_pool else "swaps_ps_received"
        checks = (("swaps_ps_pool_slot", ("pool", "slot")),) + (() if by_pool else (("swaps_ps_received", ("received_at",)),))
        for name, expected in checks:
            actual = tuple(r[2] for r in conn.execute("PRAGMA index_info(" + name + ")"))
            if actual[:len(expected)] != expected:
                raise TapeUnreadable("ps_window:index_unknown")
    except sqlite3.Error:
        raise TapeUnreadable("ps_window:index_unreadable") from None
    slot_cut = " AND (slot<? OR slot IS NULL OR typeof(slot)!='integer')" if cut_slot is not None else ""
    bound_args = (pool, lo, hi) + ((cut_slot,) if cut_slot is not None else ())
    extra = " AND (" + predicate + ")" if predicate else ""
    sql = ("WITH bounds AS (SELECT MIN(slot) AS low_slot,MAX(slot) AS high_slot,"
           "MAX(CASE WHEN typeof(slot)!='integer' OR slot<0 THEN 1 ELSE 0 END) AS bad_slot "
           "FROM swaps_ps INDEXED BY " + received_index + " WHERE pool=? AND received_at>=? AND received_at<?"
           + slot_cut + ") SELECT bounds.bad_slot,s.slot IS NOT NULL," + columns +
           " FROM bounds LEFT JOIN swaps_ps AS s INDEXED BY swaps_ps_pool_slot "
           "ON bounds.bad_slot=0 AND s.pool=? AND s.slot BETWEEN bounds.low_slot AND bounds.high_slot "
           "AND s.received_at>=? AND s.received_at<?" + extra + " ORDER BY " + order)
    args = bound_args + tuple(params) + (pool, lo, hi) + tuple(predicate_params)
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise TapeUnreadable("ps_window:row_limit")
        sql += " LIMIT ?"
        args += (limit,)
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        raise TapeUnreadable("ps_window:" + type(exc).__name__) from None
    if any(r[0] for r in rows):
        raise TapeUnreadable("ps_window:slot_unknown")
    return [tuple(r[2:]) for r in rows if r[1]]


def _proof_before(conn, pool, lo, cut_slot=None, *, identity=False):
    """The newest proven virtual quote received before `lo`, or None (none retained)."""
    columns = "payload_json,received_at,slot,signature,ordinal,tx_index" if identity else "payload_json"
    floor = retained_from(conn, pool, lo)
    for row in window_rows(conn, columns, pool, floor, lo, cut_slot=cut_slot, limit=50,
                           order="slot DESC,COALESCE(tx_index,-1) DESC,ordinal DESC",
                           predicate="is_buy=1 AND payload_json LIKE ?", predicate_params=('%' + VQ_SOURCE + '%',)):
        v = _proof(row[0])
        if v is not None:
            if not identity:
                return v
            _, at, slot, sig, ordinal, tx_index = row
            return {"value": v, "received_at": at, "slot": slot, "signature": sig, "ordinal": ordinal,
                    "tx_index": tx_index}
    return None



def sell_post(own, nxt):
    """A sell's post state for pricing. own = its decoded (base, quote) or (None, None); nxt = the next same-pool
    row's (pre_base, pre_quote) or None. A decoded post is an OBSERVATION (ix_derived): it prices only when the
    next pre confirms it. Unresolved or mismatch gives (None, None). A legacy NULL row keeps the next pre."""
    if nxt is None:
        return None, None
    if own[0] is None and own[1] is None:
        return nxt
    return own if tuple(own) == tuple(nxt) else (None, None)

def load_ps_swaps(desk_dir, mint, lo, hi, sol_usd, *, with_gaps=False, base_decimals=6, cut_slot=None,
                  max_rows=20000, integrity=False, loss_reads=None):
    """The mint's canonical-pool PumpSwap swaps with lo <= received_at < hi, oldest first, as curve-reader rows:
    {received_at, slot, signature, ordinal, is_buy, usd, price_usd, price_sol, actor, token_atomic, block_time, coin_creator}.
    [] when the PumpSwap tape does not exist (capture off) or the pool has no rows. With `with_gaps`, (rows, gaps) where
    gaps are the receipt times of this pool's LOST swaps: a proven post state that is not the next swap's pre state
    (99.3-99.9% of consecutive pairs match on the live tape). With `with_gaps` the rows also carry, for each loss, one
    price-only print (price_only=True, usd 0, actor None) of the pool right after the lost swap(s) = the next swap's pre
    state; readers count it as price, never as flow."""
    if base_decimals is not None and (type(base_decimals) is not int or not 0 <= base_decimals <= 18):
        raise TapeUnreadable("ps_swaps:decimals")
    if cut_slot is not None and (type(cut_slot) is not int or cut_slot < 0):
        raise TapeUnreadable("ps_swaps:cut_slot")
    if type(max_rows) is not int or not 0 < max_rows <= 20000:
        raise TapeUnreadable("ps_swaps:row_limit")
    scale = None if base_decimals is None else 10 ** base_decimals
    conn = _ro(desk_dir)
    if conn is None:
        return ([], []) if with_gaps else []
    pool = canonical_pool(mint)
    try:
        if integrity:
            columns = tuple(r[2] for r in conn.execute('PRAGMA index_info(swaps_ps_pool_slot)'))
            if columns[:2] != ('pool', 'slot'):
                raise TapeUnreadable('ps_swaps:index_missing')
        # CHAIN order (slot, tx_index, ordinal): a sell event carries no proven post state, but the NEXT swap's pre
        # reserves are exactly the pool after it. Nothing past `hi` is read (an as-of reader must not see the future),
        # so the window's last sell stays unpriced.
        rows = window_rows(conn, (
            'received_at, slot, signature, ordinal, is_buy, quote_atomic, base_atomic, actor, '
            'post_base_reserve_atomic, post_quote_reserve_atomic, block_time, coin_creator, quote_mint, '
            'mint, pre_base_reserve_atomic, pre_quote_reserve_atomic, CASE WHEN is_buy=1 AND payload_json '
            'LIKE ? THEN payload_json END,tx_index '
        ), pool, lo, hi, cut_slot=cut_slot, limit=max_rows + 1,
                           params=('%' + VQ_SOURCE + '%',))       # only a proof row's payload is fetched
        if len(rows) > max_rows:
            raise TapeUnreadable("ps_swaps:row_limit")
        seed = _proof_before(conn, pool, lo, cut_slot, identity=True) if rows else None
        vq = seed["value"] if seed else None
    except sqlite3.Error as exc:
        raise TapeUnreadable("ps_swaps:%s" % type(exc).__name__) from None
    finally:
        conn.close()
    out, gaps = [], []
    loss_times = []
    if integrity:
        from sniper.services import memecoin_desk_ps_losses as L
        try:
            loss_times = L.losses(desk_dir, pool, min(lo, seed["received_at"]) if seed else lo, hi,
                                 cut_slot=cut_slot, max_rows=max_rows, loss_reads=loss_reads)
        except L.LossUnreadable as exc:
            raise TapeUnreadable(str(exc)) from None
    proof_identity = seed
    if integrity and seed is not None and (type(seed['slot']) is not int or seed['slot'] < 0
                                           or type(seed['received_at']) not in (int, float)):
        raise TapeUnreadable('ps_swaps:seed_identity')
    # IDENTITY IS THE POOL (Gamma on d0ece5d6f): the canonical PDA embeds the mint, wSOL and index 0, and an UNMAPPED pool's
    # rows carry NULL mint/quote_mint (2,055/2,055 on 2DCcA4Nh's pool). Only a non-NULL mismatch is rejected.
    rows = [r for r in rows if r[13] in (None, mint) and r[12] in (None, WSOL_MINT)]
    lost_before = set()                  # indexes of rows preceded by lost swap(s) of this pool
    for i, (a, b) in enumerate(zip(rows, rows[1:])):
        if a[4] and a[8] is not None and (a[8], a[9]) != (b[14], b[15]):   # a buy's proven post; a sell's is an observation
            gaps.append(b[0])
            lost_before.add(i + 1)
    if integrity:
        loss_times = sorted(set(loss_times + gaps))
    loss_set = set(loss_times)
    for i, (at, slot, sig, ordinal, is_buy, quote, base, actor, post_b, post_q, bt, creator, qmint, rmint, _pb, _pq,
            payload, tx_index) in enumerate(rows):
        if None in (at, slot, sig, ordinal, is_buy, quote, base, actor):
            raise TapeUnreadable('ps_swaps:null_field')
        if integrity and proof_identity is not None:
            proof_key = (proof_identity['slot'], proof_identity['tx_index'] if proof_identity['tx_index'] is not None else -1,
                         proof_identity['ordinal'])
            row_key = (slot, tx_index if tx_index is not None else -1, ordinal)
            loss_index = bisect_left(loss_times, proof_identity['received_at'])
            if proof_key > row_key or (loss_index < len(loss_times) and loss_times[loss_index] <= at):
                vq = None
        proven = _proof(payload)
        if with_gaps and i in lost_before and vq is not None and scale is not None:
            # GAMMA (b): the lost swap(s) end in exactly this swap's PRE state; print that price (no flow) so the chart's
            # high/low cannot miss the excursion. Same receipt time, ordered just before this swap.
            try:
                pb, pq = int(_pb), int(_pq)
                if pb > 0 and pq + vq > 0:
                    lost_price = (pq + vq) / 1e9 / (pb / scale)
                    out.append({"received_at": at, "slot": slot, "signature": sig + ":pre", "ordinal": ordinal - 0.5,
                                "is_buy": 0, "usd": 0.0, "price_sol": lost_price,
                                "price_usd": lost_price * sol_usd if sol_usd else None, "actor": None, "token_atomic": 0,
                                "block_time": bt, "coin_creator": creator, "price_only": True})
            except (TypeError, ValueError):
                pass
        if proven is not None:
            vq = proven                      # the most recent proven virtual quote at or before this swap
            proof_identity = {'received_at': at, 'slot': slot, 'signature': sig, 'ordinal': ordinal,
                              'tx_index': tx_index, 'value': proven}
            if integrity and at in loss_set:
                vq = None
        if None in (at, slot, sig, ordinal, is_buy, quote, base, actor):
            raise TapeUnreadable("ps_swaps:null_field")
        if not is_buy:                               # a sell: priced only by the next swap's pre state (ponytail: a decode-loss
            post_b, post_q = sell_post((post_b, post_q),  # miss between them shifts the drop by one swap, never invents one)
                                       (rows[i + 1][14], rows[i + 1][15]) if i + 1 < len(rows) else None)
        price_sol = None
        try:
            pb, pq = int(post_b), int(post_q)
            if vq is not None and scale is not None and pb > 0 and pq + vq > 0:
                price_sol = (pq + vq) / 1e9 / (pb / scale)
        except (TypeError, ValueError):
            price_sol = None
        out.append({"received_at": at, "slot": slot, "signature": sig, "ordinal": ordinal, "is_buy": int(is_buy),
                    "quote_atomic": int(quote),
                    "usd": int(quote) / 1e9 * sol_usd if sol_usd else None,
                    "price_sol": price_sol, "price_usd": price_sol * sol_usd if price_sol is not None and sol_usd else None,
                    "actor": actor, "token_atomic": int(base), "block_time": bt, "coin_creator": creator,
                    "virtual_quote_proof": dict(proof_identity) if vq is not None and proof_identity else None})
    out = sorted(out, key=lambda r: (r["received_at"], r["slot"], r["ordinal"]))
    return (out, sorted(gaps)) if with_gaps else out


def pool_create_slot(desk_dir, pool, mint, hi, cut_slot):
    """The canonical pool's create_pool slot received before hi and below cut_slot, or None (absent or ambiguous).
    A pool has no swap before this slot: it is the one exact lower slot bound for first_buys."""
    conn = _ro(desk_dir)
    if conn is None:
        return None
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pools'").fetchone():
            return None
        rows = conn.execute("SELECT slot FROM pools WHERE pool=? AND mint=? AND quote_mint=? AND canonical=1 "
                            "AND received_at<? AND slot<? LIMIT 2", (pool, mint, WSOL_MINT, hi, cut_slot)).fetchall()
    except sqlite3.Error as exc:
        raise TapeUnreadable("pool_create_slot:%s" % type(exc).__name__) from None
    finally:
        conn.close()
    return rows[0][0] if len(rows) == 1 and type(rows[0][0]) is int else None


def first_buys(desk_dir, pool, lo, hi, cut_slot, max_rows=20000, floor_slot=None):
    """{signer: first buy receipt time} on `pool` with lo <= received_at < hi and slot < cut_slot: one indexed
    grouped read (no pricing). None without the tape. Raises TapeUnreadable on a read error or over max_rows signers.
    floor_slot: an exact lower slot bound (the pool's create slot). Without it the floor is a heuristic and the
    result may omit an eligible receipt: callers must treat it as a lower bound."""
    conn = _ro(desk_dir)
    if conn is None:
        return None
    try:
        # slot floor: Solana runs ~2.5 slots/s, so 3 slots per second of window reaches past `lo`; received_at
        # still decides membership exactly. Without it the pool's whole history is scanned (seconds on old pools).
        floor = floor_slot if floor_slot is not None else cut_slot - int((hi - lo) * 3) - 1500   # heuristic: lower bound only
        rows = conn.execute("SELECT actor, MIN(received_at) FROM swaps_ps INDEXED BY swaps_ps_pool_slot WHERE pool=? "
                            "AND slot>=? AND slot<? AND is_buy=1 AND received_at>=? AND received_at<? AND actor IS NOT NULL "
                            "GROUP BY actor LIMIT ?", (pool, floor, cut_slot, lo, hi, max_rows + 1)).fetchall()
    except sqlite3.Error as exc:
        raise TapeUnreadable("first_buys:%s" % type(exc).__name__) from None
    finally:
        conn.close()
    if len(rows) > max_rows:
        raise TapeUnreadable("first_buys:row_limit")
    return dict(rows)


def has_pool_rows(desk_dir, mint, lo, hi):
    """True when the coin's canonical pool has any swap received in [lo, hi): one indexed LIMIT 1 read."""
    conn = _ro(desk_dir)
    if conn is None:
        return False
    try:
        return bool(window_rows(conn, "1", canonical_pool(mint), lo, hi, limit=1))
    except sqlite3.Error as exc:
        raise TapeUnreadable("ps_rows:%s" % type(exc).__name__) from None
    finally:
        conn.close()


def ps_gap_spans(desk_dir, lo, hi):
    """Capture STOP spans [(start, end|None, cause, ended_by)] that OVERLAP [lo, hi] (an open span, end None, runs through hi). [] without the table.
    Raises TapeUnreadable on a read error."""
    conn = _ro(desk_dir)
    if conn is None:
        return []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ps_gap_spans)")}
        if not cols:
            return []                                     # a tape from before spans existed: point gaps only
        # the older two-column shape (start_at, end_at) is still a valid span table: cause and ended_by are then unknown (None)
        sel = ", ".join(c if c in cols else "NULL" for c in ("start_at", "end_at", "cause", "ended_by"))
        return [(r[0], r[1], r[2], r[3]) for r in conn.execute(
            "SELECT %s FROM ps_gap_spans WHERE start_at<=? AND (end_at IS NULL OR end_at>=?) ORDER BY start_at" % sel, (hi, lo))]
    except sqlite3.OperationalError as exc:
        raise TapeUnreadable("ps_spans:%s" % type(exc).__name__) from None
    except sqlite3.Error as exc:
        raise TapeUnreadable("ps_spans:%s" % type(exc).__name__) from None
    finally:
        conn.close()


def ps_point_gaps(desk_dir, lo, hi):
    """PumpSwap capture DISCONTINUITY times with lo <= at <= hi (worker starts, stream errors, storage stops; decode losses excluded), [] without the tape."""
    conn = _ro(desk_dir)
    if conn is None:
        return []
    try:
        return [r[0] for r in conn.execute("SELECT at FROM gaps_ps WHERE at>=? AND at<=? AND cause NOT IN (%s)"
                                           % ",".join("?" * len(DECODE_CAUSES)), (lo, hi, *DECODE_CAUSES))]
    except sqlite3.Error as exc:
        raise TapeUnreadable("ps_gaps:%s" % type(exc).__name__) from None
    finally:
        conn.close()


def ps_gap_intervals(desk_dir, lo, hi):
    """Capture STOP spans clipped to the window as closed (start, end) tuples: an open span runs through hi. Readers that test nested windows MUST use these
    (structure_tape does): a point sample can miss a window narrower than its spacing, and a stop covering the whole window leaves no point inside it."""
    return [(max(start, lo), hi if end is None else min(end, hi)) for start, end, _cause, _by in ps_gap_spans(desk_dir, lo, hi)]


def ps_gaps(desk_dir, lo, hi):
    """Point-gap times for readers that only need 'was there any outage in [lo, hi]': the point gaps PLUS each overlapping span's clipped start (and its end when
    that falls inside), so a stop [100, 300] queried over [150, 250] is an outage at 150, never 'no gap'. Readers that test NESTED windows use
    ps_point_gaps + ps_gap_intervals instead."""
    points = ps_point_gaps(desk_dir, lo, hi)
    for a, b in ps_gap_intervals(desk_dir, lo, hi):
        points.append(a)
        if lo <= b < hi:
            points.append(b)
    return sorted(set(points))


def ps_actors_of_signatures(desk_dir, sigs):
    """Actors of PumpSwap swaps in our own fill transactions (a graduated position's exit sells on the pool)."""
    sigs = [x for x in sigs if isinstance(x, str) and x]
    conn = _ro(desk_dir) if sigs else None
    if conn is None:
        return set()
    try:
        out = set()
        for i in range(0, len(sigs), 200):
            chunk = sigs[i:i + 200]
            out |= {r[0] for r in conn.execute("SELECT actor FROM swaps_ps WHERE signature IN (%s)" % ",".join("?" * len(chunk)),
                                               chunk) if r[0]}
        return out
    except sqlite3.Error as exc:
        raise TapeUnreadable("ps_ours:%s" % type(exc).__name__) from None
    finally:
        conn.close()


def bars_1s(rows):
    """1 s candles from load_ps_swaps rows, in the curve candles' shape and unit (SOL per whole token), keyed by the swap's
    block time. Unpriced rows add flow to their second but no price; a second with no priced swap gets no bar."""
    by_t = {}
    for r in rows:
        if r["block_time"] is None:
            continue
        by_t.setdefault(int(r["block_time"]), []).append(r)
    out = []
    for t in sorted(by_t):
        rs = by_t[t]
        prices = [r["price_sol"] for r in rs if r["price_sol"] is not None]
        if not prices:
            continue
        out.append({"t": t, "venue": "pumpswap", "o": prices[0], "h": max(prices), "l": min(prices), "c": prices[-1],
                    "buy_usd": sum(r["usd"] or 0.0 for r in rs if r["is_buy"]),
                    "sell_usd": sum(r["usd"] or 0.0 for r in rs if not r["is_buy"]),
                    "n_makers": len({r["actor"] for r in rs if not r.get("price_only")})})
    return out


def load_ps_states(desk_dir, mint, lo, hi):
    """The canonical pool's EXECUTABLE states with lo <= received_at < hi, oldest first, for exact sell math: [{t, q, b, fee}] where
    q = the proven post quote reserve + the proven virtual quote (the same proof rule as load_ps_swaps: a legacy buy's own virtual
    quote, carried forward, else no state), b = the post base reserve, fee = lp + protocol + creator + cashback bps / 1e4 (the buyback
    bps is a SHARE of the protocol fee, never a fee of its own: pump_curve_buy.total_fee_bps). A sell carries no post state: it takes
    the NEXT swap's pre state (the pool right after it); the window's last sell stays unpriced. A row with a NULL fee or no proof is
    dropped, never priced with a guessed value. [] without the tape or without proof. Raises TapeUnreadable on a read error."""
    conn = _ro(desk_dir)
    if conn is None:
        return []
    pool = canonical_pool(mint)
    try:
        rows = window_rows(conn, (
            'received_at, slot, ordinal, post_base_reserve_atomic, post_quote_reserve_atomic, '
            'pre_base_reserve_atomic, pre_quote_reserve_atomic, lp_fee_bps, protocol_fee_bps, '
            'creator_fee_bps, cashback_fee_bps, quote_mint, mint, CASE WHEN is_buy=1 AND payload_json LIKE ? '
            'THEN payload_json END, is_buy '
        ), pool, lo, hi, params=('%' + VQ_SOURCE + '%',))
        vq = _proof_before(conn, pool, lo) if rows else None
    except sqlite3.Error as exc:
        raise TapeUnreadable("ps_states:%s" % type(exc).__name__) from None
    finally:
        conn.close()
    rows = [r for r in rows if r[12] in (None, mint) and r[11] in (None, WSOL_MINT)]
    out = []
    for i, (at, slot, ordinal, post_b, post_q, _pb, _pq, lp, proto, creator, cashback, _q, _m, payload, is_buy) in enumerate(rows):
        proven = _proof(payload)
        if proven is not None:
            vq = proven
        if not is_buy:
            post_b, post_q = sell_post((post_b, post_q), (rows[i + 1][5], rows[i + 1][6]) if i + 1 < len(rows) else None)
        fees = [lp, proto, creator]
        if vq is None or post_b is None or post_q is None or any(f is None for f in fees):
            continue
        try:
            b, q = int(post_b), int(post_q) + vq
            fee = (sum(int(f) for f in fees) + int(cashback or 0)) / 1e4
        except (TypeError, ValueError):
            continue
        if b > 0 and q > 0:
            out.append({"t": at, "q": q, "b": b, "fee": fee})
    return out

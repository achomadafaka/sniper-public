"""m5 volume for a coin, read from OUR OWN swap tape instead of a market-data provider.

Why this exists (PERSON, 4663, 2026-09-16 11:57:28.521Z): seven swaps landed in the five minutes
before the decision, the provider had indexed ONE of them, and the gate refused a coin whose real
other-volume was about $3,480 against a $2,677 floor. A provider that indexes minutes late cannot
answer a five-minute question on a fast chain.

TWO RULES THIS MODULE WILL NOT BEND:

1. UNKNOWN IS NEVER ZERO. No known pool, a feed that has gone quiet, or a window whose every swap
   we cannot price returns `usd: None`, and the caller falls back to the provider. A confident $0
   refuses a live coin, which is the failure this module exists to remove.
   A pool we cannot price is SKIPPED and named in `unpriced_pools` (2026-09-16: one USDG-quoted
   swap voided 64% of live 4663 mints). The sum is then a LOWER BOUND, which is safe only because
   the gate reads it through max(); tests/test_memecoin_onchain_volume_gate.py pins that.
2. THE SOURCE TRADE IS EXCLUDED BY HASH. Never by subtracting its reported size from a total that
   may not contain it -- that is the arithmetic that produced "organic $407" on PERSON.
3. AN ABSENT SOURCE TRADE MEANS THE TAPE IS BLIND, NOT THAT THE COIN IS QUIET (MRLN, 4663,
   2026-09-23). The source wallet's own swap is the one transaction we know happened in the window.
   If the tape does not hold it, the tape is not watching where this coin trades, so the sum is
   evidence about our coverage rather than about the coin, and the result is `usd: None`.

It reports which pools it summed and their protocol, so a V4-only number for a coin that also
trades on V2/V3 is labelled `partial` instead of looking complete.
"""
from __future__ import annotations

import logging
import sqlite3
import math
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

WINDOW_S = 300.0
MAX_FEED_AGE_S = 120.0
COVERED_PROTOCOLS = ("v4", "v3", "pons_v2")   # what the subscription actually ingests today
                                     # (V3 since 2026-09-22, Pons v2 curves since 2026-09-23)


def covered_protocols(chain, conn=None):
    """COVERED_PROTOCOLS plus `v2` on a chain whose tape subscribes the V2 Swap topics
    (MEMECOIN_ONCHAIN_V2_SWAP_CHAINS) AND whose current socket confirmed both of them
    (`volume_feed_optional`, cleared on every reset; Beta T3). Otherwise a `v2` pool stays
    uncovered: counting it with no live subscription would report its volume as a measured zero."""
    from sniper.settings import memecoin as cfg
    if str(chain) not in cfg.memecoin_onchain_v2_swap_chains() or conn is None:
        return COVERED_PROTOCOLS
    try:
        row = conn.execute("SELECT v2_confirmed_at FROM volume_feed_optional WHERE chain_id=?",
                           (str(chain),)).fetchone()
    except sqlite3.Error:
        row = None                                   # an older store: no proof, not covered
    return COVERED_PROTOCOLS + (("v2",) if row and row[0] is not None else ())


def onchain_volume_m5(conn, chain, mint, at, *, exclude_tx=None, window_s=WINDOW_S,
                      price_fn=None, max_feed_age_s=MAX_FEED_AGE_S, protocol_lag_s=None,
                      max_protocol_lag_s=None, source_tokens=None):
    """Quote-side USD traded in the `window_s` before `at`, over this coin's covered pools.

    `source_tokens`: whole tokens the source acquired in `exclude_tx`, when known. The tape must
    hold at least SOURCE_TAPED_MIN_SHARE of them in that transaction's rows, or a venue the source
    used is not on the tape and the window is blind (rule 3, per venue)."""
    chain, mint = str(chain), str(mint).lower()
    out = {"usd": None, "swaps": 0, "newest_block_age_s": None, "pools": [],
           "coverage": "unknown", "uncovered_protocols": [], "unpriced_pools": [], "reason": None,
           "window_s": float(window_s), "excluded_tx": (exclude_tx or "").lower() or None,
           # None when no source transaction was named; otherwise whether the tape actually held
           # it. See rule 3 below.
           "excluded_tx_seen": None}

    out["protocol_lag_s"] = None
    # NULL until a protocol is known for this coin: a mint with no covered pool has no
    # subscription to be behind, so the gap is UNKNOWN rather than zero.
    out["covered_gap_s"] = None
    out["max_protocol_lag_s"] = (float(max_protocol_lag_s)
                                 if max_protocol_lag_s not in (None, "") else None)
    protocols = _pool_protocols(conn, chain, mint)
    _covered = covered_protocols(chain, conn)
    covered = [p for p in protocols if p["protocol"] in _covered]
    uncovered = sorted({p["protocol"] for p in protocols if p["protocol"] not in _covered})
    out["uncovered_protocols"] = uncovered
    if not covered:
        out["reason"] = "no_known_pool"
        return out

    # ⚠️ A SUBSCRIPTION CAN BE MINUTES BEHIND ON A SOCKET WHOSE HEADS ARE CURRENT (2026-09-23).
    # `newest_block_age_s` below is fed by `newHeads`, which stayed at 0.5 s through the whole
    # 4663 stall while the V4 subscription drifted to 652.9 s and then dropped 2,754 blocks. So a
    # coin is judged on the delivery of ITS OWN protocols: V4 pools carry 32-byte ids, V3 and Pons
    # 20-byte ones, and when several apply the WORST decides, because a missing leg understates the
    # sum either way. This can only add blindness -- the floor then falls back to the provider.
    lag, lagging = _worst_protocol_lag(covered, protocol_lag_s)
    out["protocol_lag_s"] = lag
    # HOW MUCH OF THIS WINDOW WE COULD NOT HAVE SEEN. `covered_s` is the window REQUESTED,
    # and it cannot fail: the three attempts inside the 2026-09-23 V4 stall each recorded
    # `covered_s = 300.0` with zero swaps while 2,754 blocks were missing. A protocol behind
    # by L seconds has not delivered the last L seconds of its own window, so that much of it
    # is unseen. Clamped into [0, window] -- this can only ever say we saw LESS.
    #
    # IT IS A LOWER BOUND, NOT THE WHOLE TRUTH. A span the provider delivered late and then
    # DROPPED reads 0 here once delivery recovers, because the lag is healthy again while the
    # blocks stay missing. Measuring that needs the block axis, which costs p50 17.8 ms on
    # the live tape and therefore cannot run where this runs.
    # NO MEASUREMENT IS UNKNOWN, NOT "NOTHING WAS LOST". A store the writer has never written a
    # lag into, or a rolled-back writer, must not report a window as fully seen.
    out["covered_gap_s"] = (None if lag is None
                            else max(0.0, min(float(lag), float(window_s))))
    if (out["max_protocol_lag_s"] and lag is not None and lag > out["max_protocol_lag_s"]):
        out["reason"] = "protocol_lagging"
        out["coverage"] = "blind_protocol_lagging"
        out["lagging_protocols"] = lagging
        return out
    # A SILENT SUBSCRIPTION IS NOT A QUIET ONE (2026-09-29, 8453). After a reconnect the V4
    # subscription delivered nothing while V3 on the same socket carried 1,100-3,400 swaps a minute;
    # V4's lag samples expired to NULL, the check above skipped it, and V4 coins were summed over a
    # window holding none of their swaps. With a sibling protocol MEASURED on the same writer, the
    # socket demonstrably delivers, so "no sample" for this coin's own protocol is blindness, not a
    # measured quiet. With no measurement at all (an old store, a rolled-back writer, a fresh socket)
    # the behaviour is unchanged.
    silent = _silent_protocols(covered, protocol_lag_s)
    if out["max_protocol_lag_s"] and silent:
        out["reason"] = "protocol_lagging"
        out["coverage"] = "blind_protocol_silent"
        out["lagging_protocols"] = silent
        out["covered_gap_s"] = float(window_s)
        return out

    newest_ts = _newest_block_ts(conn, chain)
    if newest_ts is not None:
        out["newest_block_age_s"] = float(at) - float(newest_ts)
    if newest_ts is None:
        out["reason"] = "no_feed"
        return out
    if out["newest_block_age_s"] < 0 or out["newest_block_age_s"] > float(max_feed_age_s):
        # The tape has gone quiet. "No swaps" and "no feed" look identical from a count alone, and
        # only one of them is a measurement.
        out["reason"] = "feed_stale"
        return out

    price_fn = price_fn or _default_price
    if out["excluded_tx"]:
        out["excluded_tx_seen"] = False
    totals, unpriced, swaps_seen = {}, {}, 0
    txn_ids, unidentified = {}, set()
    rows, orphans, unverified = _swaps(conn, chain, [p["pool"] for p in covered],
                                       float(at) - float(window_s), float(at))
    out["orphan_swaps"], out["unverified_swaps"] = orphans, unverified
    if unverified:
        # A window we could not verify is UNKNOWN, not quiet. Every row written before `block_times`
        # carried a hash reads this way until a backfill re-observes its height or it ages out of
        # the window -- at most one window -- and quietly summing the rest would report a smaller
        # number as if it were a measurement.
        out["reason"] = "hash_unverified"
        return out
    # E7's split (2026-09-29): the side of each counted swap, from the sign of its QUOTE amount
    # (positive = into the pool = a buy; V4 rows are into-pool since 05293598e). One read of the
    # covered pools' quote sides, not one per row.
    marks = ",".join("?" * len(covered))
    meta = {r["pool"]: dict(r) for r in conn.execute(
        "SELECT pool, quote_side, token0_decimals, token1_decimals, token0, token1 FROM pool_meta "
        f"WHERE chain_id=? AND pool IN ({marks})", (chain, *[p["pool"] for p in covered]))}
    quote_side_of = {pool: m["quote_side"] for pool, m in meta.items()}
    taped = 0.0
    for row in rows:
        if out["excluded_tx"] and str(row["tx_sig"] or "").lower() == out["excluded_tx"]:
            out["excluded_tx_seen"] = True
            source_meta = meta.get(row["pool"], {})
            side, d0, d1 = (source_meta.get("quote_side"), source_meta.get("token0_decimals"),
                            source_meta.get("token1_decimals"))
            try:                                  # coin OUT of the pool is negative (into-pool +)
                coin = int(row["amount1"] if side == 0 else row["amount0"]) if side in (0, 1) else 0
                taped += max(0, -coin) / 10 ** int(d1 if side == 0 else d0)
            except (TypeError, ValueError):
                pass
            continue
        quote_usd = _quote_usd(conn, chain, row, price_fn, metadata_by_pool=meta)
        if quote_usd is None:
            unpriced[row["pool"]] = unpriced.get(row["pool"], 0) + 1
            continue
        bucket = totals.setdefault(row["pool"], {"swaps": 0, "usd": 0.0, "buys": 0, "sells": 0})
        tx = str(row["tx_sig"] or "").strip().lower()
        if tx:
            txn_ids.setdefault(row["pool"], set()).add(tx)
        else:
            unidentified.add(row["pool"])
        bucket["swaps"] += 1
        bucket["usd"] += quote_usd
        swaps_seen += 1
        side = quote_side_of.get(row["pool"])
        try:
            signed = int(row["amount0"] if side == 0 else row["amount1"]) if side in (0, 1) else 0
        except (TypeError, ValueError):
            signed = 0
        if signed > 0:
            bucket["buys"] += 1
        elif signed < 0:
            bucket["sells"] += 1

    by_pool = {p["pool"]: p["protocol"] for p in covered}
    out["unpriced_pools"] = [{"pool": pool, "protocol": by_pool.get(pool, "unknown"), "swaps": n}
                             for pool, n in sorted(unpriced.items())]
    if unpriced and not totals:
        out["reason"] = "quote_not_priceable"
        return out
    out["pools"] = [{"pool": pool, "protocol": by_pool.get(pool, "unknown"),
                     "swaps": v["swaps"], "usd": v["usd"], "buys": v["buys"], "sells": v["sells"],
                     "txns": None if pool in unidentified else len(txn_ids.get(pool, ()))}
                    for pool, v in sorted(totals.items())]
    out["swaps"] = swaps_seen
    out["txns"] = (None if unidentified else len(set().union(*txn_ids.values())))
    if out["excluded_tx"] and not out["excluded_tx_seen"]:
        # The sum this window DID produce, kept so a caller holding a second instrument can decide
        # whether the absence is a venue gap or a quiet coin. `usd` stays None: this function has
        # only one instrument and must not answer a question it cannot see.
        out["source_absent_usd"] = sum(v["usd"] for v in totals.values())
        # RULE 3: THE SOURCE TRADE IS THE TAPE'S OWN COVERAGE TEST (MRLN, 4663,
        # 2026-09-23 13:02:39Z). The source wallet just traded this coin, so its transaction is a
        # swap that certainly happened inside the window. If the tape does not hold it, the tape
        # does not cover the venue the source used, and whatever it did sum came from somewhere
        # else. MRLN: $0.11 over one Uniswap pool holding $2.51, refused against a $259 floor,
        # while the coin was doing $3,721 in the same five minutes on Pons v2 -- a venue this
        # subscription does not ingest. `uncovered_protocols` was `[]` at the same moment, because
        # it can only name protocols of pools we already know.
        #
        # This says nothing about how much the coin traded, so it must not produce a number. It
        # returns the existing unknown, and the caller's `onchain_blind_undecidable` path leaves
        # the m5 floor to the provider instead of refusing on a lower bound from the wrong venue.
        # A transaction too new to have reached the tape reads the same way, which is the
        # conservative direction: measured head lag on 4663 is p50 0.80s / p90 2.02s (n=35).
        out["usd"] = None
        out["coverage"] = "blind_source_tx_absent"
        out["reason"] = "source_tx_absent"
        return out
    try:
        wanted = float(source_tokens) if source_tokens is not None else None
    except (TypeError, ValueError):
        wanted = None
    if wanted is not None and wanted > 0 and out["excluded_tx_seen"]:
        out["source_tokens_taped"] = taped
        if taped < SOURCE_TAPED_MIN_SHARE * wanted:
            # ULTRONICS: the split route's V4 leg (8% of the coin) put the transaction on the tape
            # while the Pons curve that sold the other 92% was untaped. Unknown, never a lower bound
            # from the wrong venue.
            out["usd"] = None
            out["coverage"] = "blind_source_venue_untaped"
            out["reason"] = "source_venue_untaped"
            return out
    out["usd"] = sum(v["usd"] for v in totals.values())
    out["coverage"] = "partial" if uncovered or unpriced else "complete"
    out["reason"] = "ok"
    return out


SPOT_PROTOCOLS = ("v4", "v3")        # their swap rows carry the post-swap sqrtPriceX96 (V4 as v3_swap)


def _window_anchor(conn, chain, pool, lo, since, ts, *, before_block=None):
    """(block time, sqrtPriceX96, block number) of the pool's last swap at or before `lo`, verified and
    at or after `since`; else None. A newer orphan of a reorg is skipped; an unverified row (no
    header or no hash) means the standing spot is not known, so there is no anchor.
    `before_block`, the first in-window swap's block, starts the index walk below the window, so a
    hot pool's in-window rows are never scanned (an unbounded walk measured 42 ms on 4663)."""
    bound = "" if before_block is None else " AND t.block_no<%d" % int(before_block)
    rows = conn.execute(
        f"SELECT t.sqrt_price_x96, {ts}, t.block_hash, b.block_hash, t.block_no FROM pool_ticks t "
        "LEFT JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
        f"WHERE t.chain_id=? AND t.pool=? AND t.kind='v3_swap'{bound} "
        f"AND (b.block_no IS NULL OR {ts}<=?) "
        "ORDER BY t.block_no DESC, t.log_index DESC LIMIT 8", (chain, pool, lo)).fetchall()
    for sqrt, when, th, bh, block in rows:
        if when is None or th is None or bh is None:
            return None
        if str(th).lower() != str(bh).lower():
            continue
        return (float(when), sqrt, block) if float(when) >= since else None
    return None


def own_spot_series(conn, chain, mint, at, *, since, window_s=900.0, step_s=30.0, price_fn=None,
                    max_feed_age_s=MAX_FEED_AGE_S, protocol_lag_s=None, max_protocol_lag_s=None):
    """E14 (spec 2026-10-08 funded momentum own data): the coin's USD spot on ONE pool, on a grid.

    The pool is the coin's covered V3/V4 pool with the most verified swaps in the window. The spot
    at each grid point `at - step_s*k` is the post-state of the last swap at or before it, carried
    forward: only swaps move a V3/V4 spot. Points before that pool's first swap in the window, or
    before `since` (this socket's first header, the volume gate's coverage rule), are skipped.
    A grid, not every swap: a sandwich's intra-block spike must not become the peak.

    USD = quote units x the tape's own quote price at `at`, applied to every point, so ratios are
    pure coin/quote moves. Every blind case returns `points == []` with a reason, never a flat line.
    """
    chain, mint = str(chain), str(mint).lower()
    out = {"reason": None, "points": [], "pool": None, "protocol": None, "quote_token": None,
           "quote_usd": None, "covered_s": None}
    covered = set(covered_protocols(chain, conn))
    pools = [p for p in _pool_protocols(conn, chain, mint) if p["protocol"] in covered]
    if not pools:
        out["reason"] = "no_known_pool"
        return out
    spot = [p for p in pools if p["protocol"] in SPOT_PROTOCOLS]
    if not spot:
        out["reason"] = "no_spot_pool"
        return out
    lag, _lagging = _worst_protocol_lag(spot, protocol_lag_s)
    if max_protocol_lag_s and lag is not None and lag > float(max_protocol_lag_s):
        out["reason"] = "protocol_lagging"
        return out
    if max_protocol_lag_s and _silent_protocols(spot, protocol_lag_s):
        out["reason"] = "protocol_silent"
        return out
    newest = _newest_block_ts(conn, chain)
    if newest is None:
        out["reason"] = "no_feed"
        return out
    if not 0 <= float(at) - float(newest) <= float(max_feed_age_s):
        out["reason"] = "feed_stale"
        return out
    lo = max(float(at) - float(window_s), float(since))
    if lo >= float(at):
        out["reason"] = "window_incomplete"
        return out
    out["covered_s"] = float(at) - lo
    ids = [p["pool"] for p in spot]
    marks = ",".join("?" * len(ids))
    ts = ("(CASE WHEN typeof(b.block_ts) IN ('integer','real') THEN b.block_ts "
          "ELSE unixepoch(b.block_ts) END)")
    # LEFT JOIN: a swap with no header, or a hash on either side missing, is UNVERIFIED and makes the
    # window unknown (as `_swaps`); a hash mismatch is an orphan of a reorg and is dropped.
    rows = conn.execute(
        f"SELECT t.pool, t.sqrt_price_x96, {ts} AS ts, t.block_hash AS th, b.block_hash AS bh, "
        "t.block_no "
        "FROM pool_ticks t LEFT JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
        f"WHERE t.chain_id=? AND t.pool IN ({marks}) AND t.kind='v3_swap' "
        f"AND ((b.block_no IS NULL AND t.observed_at>=?) OR ({ts}>=? AND {ts}<=?)) "
        "ORDER BY t.block_no, t.log_index",
        (chain, *ids, datetime.fromtimestamp(lo, timezone.utc).isoformat(), lo, float(at))).fetchall()
    by_pool = {}
    first_block = {}
    for pool, sqrt, when, th, bh, block in rows:
        if when is None or th is None or bh is None:
            out["reason"] = "hash_unverified"
            return out
        if str(th).lower() != str(bh).lower():
            continue
        by_pool.setdefault(pool, []).append((float(when), sqrt))
        first_block.setdefault(pool, block)
    if by_pool:
        pool, swaps = max(by_pool.items(), key=lambda kv: (len(kv[1]), kv[1][-1][0]))
    else:
        # A QUIET COIN: the spot pool that traded last, one index seek per pool. Its anchor below
        # is a measured constant across the window, or there is no series.
        last = conn.execute(f"SELECT pool, MAX(block_no) FROM pool_ticks WHERE chain_id=? "
                            f"AND pool IN ({marks}) AND kind='v3_swap' GROUP BY pool "
                            "ORDER BY 2 DESC LIMIT 1", (chain, *ids)).fetchone()
        pool, swaps = (last[0] if last else None), []
    # THE SPOT STANDING AT THE WINDOW START (Gamma on 345f327f7). Without it the series starts at the
    # first in-window swap: a spot of 100 before the window, 70 then 90 inside reads first 70, runup
    # +28.6% (hot_narrow, clip x0.75), where the truth is first 100, runup 0, giveback 10%. The last
    # verified swap at or before `lo` seeds the grid, only if it is at or after `since`: from then
    # on this socket was connected, so no swap between it and `lo` was missed.
    anchor = (_window_anchor(conn, chain, pool, lo, float(since), ts, before_block=first_block.get(pool))
              if pool else None)
    if anchor is not None:
        swaps = [anchor[:2]] + swaps
    if not swaps:
        out["reason"] = "no_swaps_in_window"
        return out
    # Carrying an anchor also requires the intervening tape before the requested window.
    start_block = anchor[2] if anchor is not None else first_block[pool]
    try:
        gap = conn.execute(
            "SELECT 1 FROM tick_gaps WHERE chain_id=? AND resolved_at IS NULL "
            "AND to_block>=? AND from_block<=(SELECT block_no FROM block_times "
            "WHERE chain_id=? AND (CASE WHEN typeof(block_ts) IN ('integer','real') "
            "THEN block_ts ELSE unixepoch(block_ts) END)<=? "
            "ORDER BY block_no DESC LIMIT 1) LIMIT 1",
            (chain, start_block, chain, float(at))).fetchone()
    except sqlite3.Error:
        out["reason"] = "gap_evidence_unavailable"
        return out
    if gap:
        out["reason"] = "tape_gap"
        return out
    from sniper.services.memecoin_paper_ticks import EvmTapeTicks, spot_usd
    meta = conn.execute("SELECT token0, token1, quote_side, token0_decimals, token1_decimals "
                        "FROM pool_meta WHERE chain_id=? AND pool=?", (chain, pool)).fetchone()
    if not meta or meta[2] not in (0, 1):
        out["reason"] = "pool_meta_incomplete"
        return out
    quote_side, coin_side = int(meta[2]), 1 - int(meta[2])
    quote_token = str(meta[quote_side]).lower()
    # pool_meta keeps decimals on few rows (88% of 4663 and 99% of 8453 carry none, 2026-10-08);
    # the paper tick engine's resolver adds native, the chain quote and the tape's token table.
    coin_decimals = EvmTapeTicks.decimals(conn, chain, meta[coin_side], meta[3 + coin_side])
    quote_decimals = EvmTapeTicks.decimals(conn, chain, quote_token, meta[3 + quote_side])
    if coin_decimals is None or quote_decimals is None:
        out["reason"] = "decimals_unknown"
        return out
    quote_usd = (price_fn or _default_price)(chain, quote_token)
    if quote_usd is None or not math.isfinite(float(quote_usd)) or float(quote_usd) <= 0:
        out["reason"] = "quote_not_priceable"
        return out
    priced = []
    for when, sqrt in swaps:
        usd = spot_usd(sqrt, mint_is_c1=coin_side == 1, coin_decimals=coin_decimals,
                       quote_decimals=quote_decimals, quote_usd=float(quote_usd))
        if usd is not None:
            priced.append((when, usd))
    if not priced:
        out["reason"] = "no_swaps_in_window"
        return out
    points, i = [], 0
    for k in range(int(float(window_s) // float(step_s)), -1, -1):
        grid = float(at) - float(step_s) * k
        if grid < lo or grid < priced[0][0]:
            continue
        while i + 1 < len(priced) and priced[i + 1][0] <= grid:
            i += 1
        points.append((grid, priced[i][1]))
    protocol = next(p["protocol"] for p in spot if p["pool"] == pool)
    out.update(reason="ok", points=points, pool=pool, protocol=protocol, quote_token=quote_token,
               quote_usd=float(quote_usd))
    return out


def venue_gap_verdict(onchain_usd, provider_organic_usd, *, min_usd, ratio):
    """Is an absent source transaction a VENUE GAP or a QUIET COIN? Returns (blind, detail).

    `excluded_tx_seen` is false whenever the source's swap did not land on a pool we know for this
    mint, and that has at least three causes: a venue we do not ingest (MRLN, 2026-09-23, trading
    $3,721 on Pons v2 while our covered pool held $2.51), a pool of this coin we have not mapped,
    and a multi-hop route whose own leg produced no tick (MSTR, 4663, whose transaction WAS in the
    tape 0.88 s before the gate ran, on two V3 pools of other tokens). None of them is "not yet
    indexed", which the MSTR evidence rules out.

    Treating all of them as blind stops the m5 floor binding at all: 4 of 4 EVM attempts after the
    2026-09-23 15:29Z deploy read absent, against a pre-fix baseline of 158 `ok` in 240 substages on
    4663. Treating all of them as covered refuses MRLN on $0.11 from the wrong venue.

    THE PROVIDER SEPARATES THEM, AND THE GATE ALREADY HOLDS IT. Blind only when the provider's
    organic m5 -- after the existing source-exclusion and index-lag rule -- exceeds ours by BOTH a
    ratio and an absolute gap. MRLN: $0.11 against $3,721, blind. ILY (4663, 16:21:38Z): $0 against
    $5.13 whose single transaction IS the source's own buy, covered, and the floor decides.

    UNKNOWN IS NEVER QUIET. A provider value that is missing, None, NaN or negative stays blind: a
    number we could not read must not become evidence that a coin is dead.

    Ours can be 0.0, so the ratio leg is written as a multiplication and never a division. With
    ours at zero every positive provider clears it, and the absolute gap is then the only thing
    separating quiet from blind -- that is where the operator's number does the work.

    FAILURE DIRECTION, STATED: a provider reading wrong-LOW makes a live coin look quiet and we
    refuse it, losing an entry. A provider reading wrong-HIGH keeps us blind and defers to it,
    which is where this path already stood. Refusing is the cheaper error.
    """
    detail = {"onchain_usd": None, "provider_organic_m5_usd": None,
              "venue_gap_min_usd": float(min_usd), "venue_gap_ratio": float(ratio),
              "verdict": "venue_gap_blind"}
    # DISABLED MEANS BLIND, EXPLICITLY. Zeroing the knobs must restore "every absent source
    # transaction is blind", and arithmetic alone does not do that: with both at 0 and a coin where
    # ours and the provider are both 0, `provider - ours > 0` is false and the row would read
    # COVERED -- the opposite of off. The operator's rollback has to mean what it says.
    if float(min_usd) <= 0 or float(ratio) <= 0:
        detail["onchain_usd"] = _finite(onchain_usd)
        detail["provider_organic_m5_usd"] = _finite(provider_organic_usd)
        detail["venue_gap_reason"] = "rule_disabled"
        return True, detail
    ours = _finite(onchain_usd)
    provider = _finite(provider_organic_usd)
    detail["onchain_usd"] = ours
    detail["provider_organic_m5_usd"] = provider
    if provider is None or ours is None:
        detail["verdict"] = "venue_gap_blind"
        detail["venue_gap_reason"] = ("provider_unknown" if provider is None else "onchain_unknown")
        return True, detail
    blind = bool(provider > ours * float(ratio) and (provider - ours) > float(min_usd))
    detail["verdict"] = "venue_gap_blind" if blind else "quiet_covered"
    return blind, detail


def _finite(value):
    """A usable non-negative float, or None. NaN, infinity and negatives are not readings."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def _pool_protocols(conn, chain, mint):
    try:
        return [{"pool": r[0], "protocol": str(r[1] or "unknown")} for r in conn.execute(
            "SELECT pool, protocol FROM onchain_pool_protocols WHERE chain_id=? AND mint=?",
            (chain, mint))]
    except Exception:
        logger.exception("onchain volume: pool lookup failed chain=%s mint=%s", chain, mint)
        return []


# WHICH SUBSCRIPTION CARRIES THIS POOL. `onchain_pool_protocols.protocol` already says it, and
# it is the authority here -- the pool-id length discriminator exists for `pool_ticks.kind`,
# where `v3_swap` labels both families and nothing else can tell them apart.
# `v2` rides a topic-only subscription on the same socket as V3 and has no lag of its own: a V2
# subscription that stalls while V3 flows under-counts (a lower bound, the strict side).
_SUBSCRIPTION_OF = {'v4': 'v4', 'v3': 'v3', 'pons_v2': 'pons', 'v2': 'v3'}
# PONS HAS ITS OWN DELIVERY KEY (ULTRONICS, 4663, 2026-10-07). Its topic-only subscription was
# confirmed and delivered nothing (0 frames in 60 s against 119 chain events) while V3 on the same
# socket delivered 668; noted under 'v3', the dead subscription read healthy. A store written before
# the writer measured Pons separately has no 'pons' key and keeps the old 'v3' reading.
SOURCE_TAPED_MIN_SHARE = 0.8


def _subscription_key(protocol, lags):
    key = _SUBSCRIPTION_OF.get(str(protocol or "").lower())
    return 'v3' if key == 'pons' and 'pons' not in lags else key


def _worst_protocol_lag(covered_pools, protocol_lag_s):
    """(worst median delivery lag over the protocols this coin trades on, their names).

    `protocol_lag_s` is what the WRITER measured and persisted; computing it here from the tape
    costs p50 17.8 ms on 4663, which is a synchronous read this project refuses to add to the entry
    path. None for a protocol means no evidence yet, which is unknown and never "fresh": it cannot
    raise the worst lag, and a caller with no measurements at all simply keeps today's behaviour.
    """
    lags = dict(protocol_lag_s or {})
    if not lags:
        return None, []
    worst, names = None, []
    for pool in covered_pools or ():
        key = _subscription_key(pool.get("protocol"), lags)
        if key is None:
            continue
        value = lags.get(key)
        if value is None:
            continue
        if worst is None or float(value) > worst:
            worst = float(value)
        if key not in names:
            names.append(key)
    return worst, names


def own_sell_share_split(m5):
    """E7's input from this coin's own tape read, or None when the window is not a measurement.

    The pool with the most quote USD in the window stands in for DexScreener's pair pick (84.4%
    the same pool on 4663, n=141; an all-pools count runs up to 20x on arbitrage pools). Counts are
    swap events on that pool, the source's own transaction excluded (Alpha D-E7-2). Only a measured
    window splits: `usd` present and `reason` ok (the runner's quiet source-absent case included).
    """
    # 56's `counts_only` window: the receipt named the pool and the coin's side, the base has no USD
    # here. Its counts are a measurement; its volume is not.
    counts_only = isinstance(m5, dict) and m5.get("reason") == "counts_only"
    if not isinstance(m5, dict) or (not counts_only and (m5.get("usd") is None
                                                         or m5.get("reason") != "ok")):
        return None
    pools = [p for p in (m5.get("pools") or ()) if p.get("buys") is not None]
    if not pools:
        return None
    pick = max(pools, key=lambda p: (float(p.get("usd") or 0.0), str(p.get("pool") or "")))
    return {"txns_m5_buys": int(pick["buys"]), "txns_m5_sells": int(pick["sells"]),
            "pool": pick.get("pool"), "covered_s": m5.get("covered_s")}


def own_v4_depth_usd(chain, pool_ids, *, path=None, now_ts=None):
    """{pool: USD or None}: each V4 pool's quote-side VIRTUAL reserve at its latest swap on our tape.

    E10 (2026-09-29): the depth DexScreener supplied for ranking V4 candidates, from our own tape.
    With the swap's active liquidity L and sqrtPriceX96 S, the quote-side reserve is L * 2^96 / S
    (quote = token0) or L * S / 2^96 (quote = token1), in the quote's units, priced by the tape's own
    `volume_quote_prices` within the m5 gate's hop-price age (MEMECOIN_ONCHAIN_M5_HOP_PRICE_MAX_AGE_S;
    base prices are rewritten every 30 s, and a rank within one mint barely moves with ETH over
    three minutes). It is ACTIVE liquidity at the price, not TVL: compare ranks, never values. A pool
    without a swap in the tape's hour, an unpriced or stale quote or unreadable fields is None
    (unknown), never 0. Read-only; one indexed SELECT per pool (idx_pool_ticks_pool_kind_time); a
    busy database waits at most 50 ms. Never raises: a failed read returns every pool None."""
    out = {str(p).lower(): None for p in (pool_ids or ())}
    if not out:
        return out
    now_ts = time.time() if now_ts is None else float(now_ts)
    try:
        from sniper.settings.memecoin import MEMECOIN_ONCHAIN_M5_HOP_PRICE_MAX_AGE_S as max_age_s
        if path is None:
            from sniper.services.memecoin_volume_writer import db_path
            path = db_path()
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=0.05)
    except Exception:
        return out
    try:
        prices = {}
        for pool in out:
            row = conn.execute(
                "SELECT t.liquidity, t.sqrt_price_x96, m.quote_side, m.token0, m.token1, "
                "m.token0_decimals, m.token1_decimals FROM pool_ticks t JOIN pool_meta m "
                "ON m.chain_id=t.chain_id AND m.pool=t.pool WHERE t.chain_id=? AND t.pool=? "
                "AND t.kind='v3_swap' AND t.liquidity IS NOT NULL AND t.sqrt_price_x96 IS NOT NULL "
                "ORDER BY t.observed_at DESC LIMIT 1", (str(chain), pool)).fetchone()
            if row is None or row[2] not in (0, 1):
                continue
            try:
                liq, sqrt_p, side = int(row[0]), int(row[1]), int(row[2])
                decimals = int(row[5] if side == 0 else row[6])
            except (TypeError, ValueError):
                continue
            if liq < 0 or sqrt_p <= 0 or not 0 <= decimals <= 36:
                continue
            quote = str(row[3] if side == 0 else row[4]).lower()
            if quote not in prices:
                p = conn.execute("SELECT usd, observed FROM volume_quote_prices WHERE chain_id=? "
                                 "AND token=?", (str(chain), quote)).fetchone()
                fresh = (p is not None and p[0] is not None and p[1] is not None
                         and 0 <= now_ts - float(p[1]) <= float(max_age_s))
                prices[quote] = float(p[0]) if fresh else None
            usd = prices[quote]
            if usd is None or not math.isfinite(usd) or usd <= 0:
                continue
            raw = liq * (2 ** 96) / sqrt_p if side == 0 else liq * sqrt_p / (2 ** 96)
            value = raw / 10 ** decimals * usd
            out[pool] = value if math.isfinite(value) else None
    except Exception:
        return {pool: None for pool in out}
    finally:
        conn.close()
    return out


def _silent_protocols(covered_pools, protocol_lag_s):
    """This coin's own subscriptions with NO lag sample while another subscription on the same
    writer has one: delivery is proven on the socket, so their silence is not a measurement."""
    lags = dict(protocol_lag_s or {})
    if not any(value is not None for value in lags.values()):
        return []
    names = []
    for pool in covered_pools or ():
        key = _subscription_key(pool.get("protocol"), lags)
        if key is not None and key in lags and lags[key] is None and key not in names:
            names.append(key)
    return names


def _newest_block_ts(conn, chain):
    # THE NEWEST HEIGHT, BY PRIMARY KEY (Gamma, 2026-10-09). MAX(block_ts) read every header of the
    # chain: p50 3.98 ms / p90 4.46 ms on 4663 (38.5k rows), synchronously on every entry's gate
    # read. One row per height, REPLACEd on a reorg, and block times never fall with height, so the
    # newest height's time is the same value: p50/p90 0.007 ms.
    row = conn.execute("SELECT block_ts FROM block_times WHERE chain_id=? "
                       "ORDER BY block_no DESC LIMIT 1", (chain,)).fetchone()
    if not row or row[0] is None:
        return None
    value = row[0]
    try:
        return float(value)
    except (ValueError, TypeError):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _swaps(conn, chain, pools, lo_ts, hi_ts):
    """Swaps on these pools in the window, over VERIFIED canonical identity, plus the count of rows
    the verification excluded.

    ⚠️ A HEIGHT IS NOT AN IDENTITY (2026-09-23 review, F1). A reorg resets coverage and the backfill
    re-inserts the canonical swaps, but the swaps stored from the replaced block stay on disk and
    their `removed` notice may never arrive. `insert_ticks` cannot dedupe them -- they are different
    transactions -- so a join on `block_no` alone sums both: the review's fixture reads $4,800 over
    two rows where the canonical chain holds $2,400 over one.

    `block_times` keeps one row per height and is REPLACEd on a reorg, so its hash is the canonical
    one and an orphan tick simply stops matching. Nothing is deleted; the orphan stays readable.

    TWO KINDS OF EXCLUSION, AND ONLY ONE OF THEM IS AN UNKNOWN. A tick whose hash MISMATCHES the
    canonical header is a decided fact -- that block is not on the chain -- so it is excluded and
    counted as an orphan. A tick where either side carries NO hash cannot be judged at all; it is
    counted as unverified, and the caller turns a non-zero count into an explicit unknown. A NULL
    hash means unverified, never "no swaps" -- which after 225795fc would be a refusal, not a blind.

    Returns (rows, orphan_count, unverified_count).
    """
    if not pools:
        return [], 0, 0
    marks = ",".join("?" * len(pools))
    window = (f"t.chain_id=? AND t.pool IN ({marks}) "
              "AND (CASE WHEN typeof(b.block_ts) IN ('integer','real') THEN b.block_ts "
              "ELSE unixepoch(b.block_ts) END)>? AND (CASE WHEN typeof(b.block_ts) "
              "IN ('integer','real') THEN b.block_ts ELSE unixepoch(b.block_ts) END)<=? "
              "AND t.kind IN ('v3_swap','v4_swap','pons_v2_swap','v2_swap')")
    verified = "lower(t.block_hash) = lower(b.block_hash)"
    args = (chain, *pools, lo_ts, hi_ts)
    rows = conn.execute(
        "SELECT t.pool, t.tx_sig, t.amount0, t.amount1, b.block_ts "
        "FROM pool_ticks t JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
        f"WHERE {window} AND {verified} ORDER BY t.block_no, t.log_index", args).fetchall()
    known = "t.block_hash IS NOT NULL AND b.block_hash IS NOT NULL"
    orphans = conn.execute(
        "SELECT COUNT(*) FROM pool_ticks t "
        "JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
        f"WHERE {window} AND {known} AND NOT {verified}", args).fetchone()[0]
    unverified = conn.execute(
        "SELECT COUNT(*) FROM pool_ticks t "
        "JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
        f"WHERE {window} AND NOT ({known})", args).fetchone()[0]
    return rows, int(orphans or 0), int(unverified or 0)


def _quote_usd(conn, chain, row, price_fn, *, metadata_by_pool=None):
    """The QUOTE side of one swap in USD. The quote side is the pool's, not a guess from signs."""
    if metadata_by_pool is None:
        meta = conn.execute(
            "SELECT token0, token1, quote_side, token0_decimals, token1_decimals FROM pool_meta "
            "WHERE chain_id=? AND pool=?", (chain, row["pool"])).fetchone()
    else:
        # This measurement already read these pools; a missing row remains unknown.
        meta = metadata_by_pool.get(row["pool"])
    if not meta:
        return None
    if meta["quote_side"] not in (0, 1):
        return None
    quote_side = int(meta["quote_side"])
    token = meta["token0"] if quote_side == 0 else meta["token1"]
    decimals = meta["token0_decimals"] if quote_side == 0 else meta["token1_decimals"]
    raw = row["amount0"] if quote_side == 0 else row["amount1"]
    try:
        if decimals is None or not 0 <= int(decimals) <= 255:
            return None
        amount = abs(int(raw)) / (10 ** int(decimals))
    except (TypeError, ValueError):
        return None
    price = price_fn(chain, token)
    if price is None:
        return None
    try:
        value = amount * float(price)
        return value if math.isfinite(value) and math.isfinite(float(price)) and float(price) > 0 else None
    except (TypeError, ValueError):
        return None


def _default_price(chain, token):
    """USD price for a pool's QUOTE token, or None when we cannot price it honestly.

    THE STABLE CONNECTORS WERE MISSING AND THAT BLINDED HALF THE CHAIN (2026-09-22, GIGABYTE ref
    51a1a1b6). This priced only the CHAIN_QUOTE token and the native coin, so on 4663 every pool
    quoted in USDG answered None and its swaps were counted as UNPRICEABLE rather than as volume.
    Measured on pool_meta for 4663: 27,372 native and 4,468 WETH pools priced, against 17,366 USDG
    pools that could not be. GIGABYTE's own pool showed 51 swaps and priced none of them, so the
    gate fell back to the provider's $309 and refused on the m5 floor.

    USDG is already named in MEMECOIN_ONCHAIN_M5_USD_STABLES, which the Solana/Base observer reads;
    this path simply never consulted it. A configured stable is worth $1.00 by definition — that is
    what makes it the unit — so no feed is needed and none is invented.

    NOT EVERY QUOTE TOKEN IS PRICEABLE, and one on this chain proves it. Pool 0xf40edb0e is quoted
    in 0x86923f96303d656e4aa86d9d42d1e57ad2023fdc, which reads symbol "AMD", 18 decimals — a
    TOKENIZED STOCK. It is neither a stable nor a wrapper, its USD value moves with an equity we do
    not track, and pricing it at anything would be a fabrication. It stays None, and its swaps stay
    unpriceable, which is the honest answer: unknown is never zero.
    """
    try:
        from sniper.execution.evm_wallet import CHAIN_QUOTE, quote_usd_price, native_is_quote
        token = str(token).lower()
        quote = CHAIN_QUOTE.get(str(chain), {})
        supported = {str(quote.get("token", "")).lower()}
        if quote.get("kind") == "eth" or native_is_quote(chain):
            supported.add("0x" + "00" * 20)
        if token in supported:
            return quote_usd_price(chain)
        if token in _usd_stables(chain):
            return 1.0
        return None
    except Exception:
        logger.exception("onchain volume: quote price unavailable chain=%s token=%s", chain, token)
        return None


def _usd_stables(chain):
    """The configured immutable USD stables for `chain`, lowercased. Empty on any failure."""
    try:
        from sniper.settings import memecoin as cfg
        return {str(t).lower()
                for t in (cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get(str(chain)) or ())}
    except Exception:
        return set()

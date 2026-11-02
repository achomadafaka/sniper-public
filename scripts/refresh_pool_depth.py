#!/usr/bin/env python3
"""Fill `memecoin_pool_depth` from our own pool store, off the funded hot path.

WHY A SEPARATE PASS. Measured 2026-09-22 on chain 4663: the market-data provider returned no pair
at all on 1,982 of 2,254 funded entry attempts since 09-15, so `liquidity_class` read `unknown`
while `pool_meta` already knew pools for 2,060 of them. Reading those pools at decision time is not
an option — a coin carries dozens and nearly all are empty, so the deep one is usually not in the
first few, and finding it means seconds of RPC inside a decision measured in a coin's first 60
seconds. This pass does that work in advance; the entry path then does one indexed SELECT.

WHAT IT WRITES. `depth_usd` is `2 * L * sqrt(P)` over the deepest pool whose counter unit we can
price, in USD. It is a FLOOR on depth, not the provider's TVL: measured against the provider on
coins where both existed, ours read a median 0.27 of theirs. Callers label it `liquidity_source`.

READ-ONLY ON CHAIN, and it touches no funded execution path. Rate-limited, bounded per coin and
per run, and it never uses the custody endpoint.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services.memecoin_pool_depth import (  # noqa: E402
    depth_for_pools, pool_state_row, pool_state_slots)

EXTSLOAD = "0x1e2eaeaf"                  # extsload(bytes32) -- one slot, one call
# ONE CALL PER POOL INSTEAD OF TWO (2026-09-23). slot0 sits at the pool's state root and liquidity
# at root+3 (memecoin_pool_depth.POOLS_SLOT=6, LIQUIDITY_OFFSET=3), so both live inside four
# CONSECUTIVE slots and one multi-slot read returns them together. Measured before the change: 608
# pools per run at p50 x 2 calls x the 0.25 s pause below = 304 s of sleep in a 413 s run, against
# a 300 s cron period that `flock -n` then skipped every other time. Halving the COUNT keeps the
# call RATE and the provider exactly as they were.
EXTSLOAD_MULTI = "0x35fd631a"            # extsload(bytes32,uint256) -- keccak-derived, and the
                                         # same derivation reproduces EXTSLOAD above
POOL_STATE_WORDS = 4                     # slot0 .. liquidity inclusive
DEFAULT_RPC = "https://robinhood-rpc.publicnode.com"


def decode_pool_state(raw):
    """(sqrt_price_x96, liquidity) from an `extsload(bytes32,uint256)` return, or (None, None).

    The return is an ABI `bytes32[]`: a 32-byte offset, a 32-byte LENGTH, then that many words.
    Word 0 is slot0 and word 3 is liquidity.

    DECODE BY LENGTH, and never let a short answer become a zero. A provider can return fewer
    words than it declares with no error at all -- that shape returned 24 swaps on one run and 22
    on the next for the same coin on 2026-09-22, and a count taken from it was about to be
    published as chain truth. Here a short, reverted, empty or `0x` answer is UNKNOWN, so the pool
    is skipped; reading it as zero depth would let the impact gate fail open on a pool it never
    actually read.
    """
    if not raw or not isinstance(raw, str):
        return None, None                      # transport failure, revert, or a missing result
    body = raw[2:] if raw.startswith("0x") else raw
    if len(body) < 128:                        # offset and length must both be present
        return None, None                      # "0x" and any stub land here
    try:
        count = int(body[64:128], 16)
    except ValueError:
        return None, None
    words = body[128:]
    # Both conditions matter: the header must PROMISE at least four words, and the payload must
    # actually CARRY the number it promised. Either one alone accepts a truncated answer.
    if count < POOL_STATE_WORDS or len(words) < count * 64:
        return None, None
    try:
        slot0 = int(words[0:64], 16)
        liquidity = int(words[3 * 64:4 * 64], 16)
    except ValueError:
        return None, None
    sqrt_price = slot0 & ((1 << 160) - 1)
    liq = liquidity & ((1 << 128) - 1)
    if not sqrt_price or not liq:
        return None, None                      # an uninitialised pool reads zero; that is not depth
    return sqrt_price, liq


def _call(rpc_url, to, data, *, timeout=15.0, pause=0.25):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    try:
        request = urllib.request.Request(
            rpc_url, data=body,
            headers={"content-type": "application/json", "user-agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read()).get("result")
    except Exception:
        return None
    finally:
        time.sleep(pause)


def pool_rows(volume_conn, chain, mint, priced, *, limit):
    """Candidate V4 pools for `mint` whose counter unit we can price, deepest-first is unknown
    here, so the caller reads them all up to `limit`."""
    rows = []
    for pool, token0, token1, dec0, dec1 in volume_conn.execute(
            "SELECT pool, token0, token1, token0_decimals, token1_decimals FROM pool_meta "
            "WHERE chain_id=? AND (lower(token0)=? OR lower(token1)=?)",
            (str(chain), mint, mint)):
        counter = (token1 if str(token0).lower() == mint else token0).lower()
        if counter not in priced or len(str(pool)) < 60:
            continue                      # only V4 pool ids are readable through extsload here
        rows.append({"pool": pool, "token0": str(token0).lower(), "token1": str(token1).lower(),
                     "quote": counter,
                     "quote_decimals": (dec0 if str(token0).lower() == counter else dec1)})
        if len(rows) >= limit:
            break
    return rows


def _iso_utc(epoch):
    """A UTC ISO stamp in the same shape `real_utc_now_iso` produces, so the two compare as text."""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc).isoformat()


def usable_quote_prices(rows, *, now=None, max_age_s=None):
    """`(token -> usd, skipped_count)` from `volume_quote_prices` rows, plus, when `now` is given,
    `(prices, skipped, observed, oldest_used)` with every stale unit dropped.

    ⚠️ ONE UNPRICEABLE ROW MUST NOT KILL THE PASS (2026-09-23 review, F3). `usd` is nullable, and
    the comprehension that built this map called `float()` on it directly, so a single NULL raised
    TypeError before one pool had been read and the whole refresh died -- including every coin whose
    stablecoin pricing was perfectly valid.

    An unusable price makes the token ABSENT from the map, never 0.0: a zero-priced quote unit would
    value its pools at nothing and write a confident $0 depth. NaN and non-positive numbers are
    treated exactly like NULL, for the same reason.

    ⚠️ A FRESH POOL READ CANNOT MAKE AN OLD CONVERSION PRICE FRESH (2026-09-23 review, F4). With
    `max_age_s` the same rule applies to the price's AGE: on 4663 the oldest cached quote unit was
    74,687 s old (20.7 h) while the newest was 18.6 s, and the refresh valued pools with either and
    stamped the result with the wall clock. A stale unit is UNKNOWN, so it leaves the map and its
    pools go unvalued; a NULL `observed` is treated as infinitely old, for the same reason a NULL
    price is unusable.
    """
    prices, skipped, observed = {}, 0, {}
    for row in rows:
        token, usd = row[0], row[1]
        seen = row[2] if len(row) > 2 else None
        try:
            value = float(usd)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if value != value or value <= 0:
            skipped += 1
            continue
        key = str(token).lower()
        if max_age_s is not None and now is not None:
            try:
                seen_at = float(seen)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if seen_at != seen_at or float(now) - seen_at > float(max_age_s):
                skipped += 1
                continue
            observed[key] = seen_at
        prices[key] = value
    if max_age_s is None or now is None:
        return prices, skipped
    return prices, skipped, observed, (min(observed.values()) if observed else None)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", default="4663")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="never the custody endpoint")
    ap.add_argument("--since", default=None, help="only coins seen in attempts at or after this")
    ap.add_argument("--max-coins", type=int, default=40)
    ap.add_argument("--max-pools", type=int, default=40, help="pools read per coin")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="decode cases only; no DB, no network")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    import sniper.persistence.db_memecoin as m
    import sniper.settings.memecoin as cfg
    from sniper.persistence.db_core import get_db_path, get_readonly_connection
    from sniper.util.clock import real_utc_now_iso

    # STATE, NOT SOURCE (2026-09-22): this script runs release-pinned from .runtime/, where there is
    # no data/. The volume store lives under the state root, like every other data file.
    from sniper.util.state_paths import state_root
    volume = sqlite3.connect("file:%s?mode=ro" % (state_root() / "data/memecoin_onchain_volume.db"),
                             uri=True, timeout=5)
    now_epoch = time.time()
    # THE GATE'S OWN CEILING, not a second knob: a price older than the age at which a depth row
    # stops answering `pool_impact_inputs` can only ever produce a row that is already too old.
    max_price_age_s = float(getattr(cfg, "MEMECOIN_POOL_DEPTH_MAX_AGE_S", 0.0) or 0.0)
    priced, unpriceable, price_observed, _oldest = usable_quote_prices(
        volume.execute("SELECT token, usd, observed FROM volume_quote_prices WHERE chain_id=?",
                       (a.chain,)),
        now=now_epoch, max_age_s=max_price_age_s)
    if unpriceable:
        print("skipped %d quote unit(s) with no usable price" % unpriceable)
    if not priced:
        print("no priced quote units for chain %s; nothing can be valued" % a.chain)
        return 1

    since = a.since or "2026-01-01"
    # closing(), not a bare `with`. sqlite3's own context manager ends the TRANSACTION and
    # leaves the handle OPEN, so the read connection outlived this block for the whole run.
    with contextlib.closing(get_readonly_connection()) as conn:
        coins = []
        for (blob,) in conn.execute(
                "SELECT classification_json FROM memecoin_entry_attempts "
                "WHERE network_id=? AND decision_started_at>=? AND classification_json IS NOT NULL "
                "ORDER BY decision_started_at DESC", (a.chain, since)):
            try:
                mint = str((json.loads(blob or "{}")).get("mint") or "").lower()
            except Exception:
                continue
            if mint and mint not in coins:
                coins.append(mint)
            if len(coins) >= a.max_coins:
                break

    print("chain %s: %d coins, %d priced units, rpc %s"
          % (a.chain, len(coins), len(priced), a.rpc.split("//")[-1].split("/")[0]))
    written = 0
    write_conn = None if a.dry_run else sqlite3.connect(get_db_path(), timeout=10)
    try:
        for mint in coins:
            rows = pool_rows(volume, a.chain, mint, priced, limit=a.max_pools)
            if not rows:
                continue

            # KEEP EACH POOL'S STATE, not just the depth it implies. The impact gate reads
            # liquidity and sqrt_price for the pool we would trade, and the first version of this
            # script wrote neither (2026-09-23): `record_pool_depth` was called without `state=`,
            # so every cached row answered `pool_impact_inputs` with None and the gate could not
            # fire on any coin. Enabled and effective are different things.
            seen_state = {}

            def state_fn(row):
                slot0_key, _liquidity_key = pool_state_slots(row["pool"])
                raw = _call(a.rpc, _manager(a.chain),
                            EXTSLOAD_MULTI + slot0_key + "%064x" % POOL_STATE_WORDS)
                sqrt_price, liq = decode_pool_state(raw)
                if sqrt_price is None:
                    return None, None
                seen_state[row["pool"]] = (sqrt_price, liq)
                return sqrt_price, liq

            depth, pool = depth_for_pools(rows, state_fn=state_fn, price_fn=priced.get)
            print("  %s pools=%-3d depth=%s" % (mint[:14], len(rows),
                                                ("$%.0f" % depth) if depth else "none"))
            if depth is None or a.dry_run:
                continue
            best_row = next((r for r in rows if r["pool"] == pool), None)
            counter = best_row["quote"] if best_row else None
            sqrt_price, liq = seen_state.get(pool, (None, None))
            state = pool_state_row(best_row or {}, sqrt_price, liq,
                                   priced.get(counter)) if best_row else None
            # ⚠️ THE ROW IS AS OLD AS ITS OLDEST EVIDENCE (2026-09-23 review, F4). A fresh pool read
            # cannot make an old conversion price fresh, and stamping the wall clock on a depth
            # derived from a 20-hour-old quote unit let it satisfy every downstream freshness check.
            # `as_of` carries the oldest constituent instead, so `pool_impact_inputs` rejects it on
            # age by the rule it already applies -- no new column and no new reader.
            price_seen = price_observed.get(str(counter).lower()) if counter else None
            as_of_iso = real_utc_now_iso()
            if price_seen is not None and price_seen < now_epoch:
                as_of_iso = _iso_utc(price_seen)
            if state is None:
                print("     state incomplete for the deepest pool; the row will not answer the "
                      "impact gate")
            m.record_pool_depth(write_conn, a.chain, mint, depth_usd=depth, pool=pool,
                                quote=counter, pools_read=len(rows), now_iso=as_of_iso,
                                state=state)
            written += 1
    finally:
        if write_conn is not None:
            write_conn.close()
    print("wrote %d rows%s" % (written, " (dry run: none)" if a.dry_run else ""))
    return 0


def _manager(chain):
    from sniper.execution.evm_v4_quote import V4
    return V4[str(chain)]["pool_manager"]


def selftest():
    """The decode cases, offline. No database, no network, no chain read.

    The selector check is the important one: it derives BOTH selectors from their signatures and
    asserts the single-slot one reproduces the constant this script has always used. That validates
    the derivation, so the multi-slot selector is trustworthy without a call to the chain.
    """
    from eth_utils import keccak
    assert "0x" + keccak(text="extsload(bytes32)")[:4].hex() == EXTSLOAD, "derivation is wrong"
    assert "0x" + keccak(text="extsload(bytes32,uint256)")[:4].hex() == EXTSLOAD_MULTI

    head = "%064x" % 32 + "%064x" % POOL_STATE_WORDS        # ABI offset, then the LENGTH
    sqrt_price, liquidity = 79228162514264337593543950336, 123456789
    words = ["%064x" % sqrt_price, "%064x" % 1, "%064x" % 2, "%064x" % liquidity]

    # 1. a full four-word return decodes to word[0] and word[3].
    assert decode_pool_state("0x" + head + "".join(words)) == (sqrt_price, liquidity)
    # 2. a SHORT return -- the header promises four words and only two arrive. This is the shape a
    #    provider gives with no error at all, and it must never become a zero.
    assert decode_pool_state("0x" + head + "".join(words[:2])) == (None, None)
    # 3. "0x", an empty result, and a missing result (a revert or a transport failure).
    for empty in ("0x", "", None, "0x" + "00" * 31):
        assert decode_pool_state(empty) == (None, None), empty
    # 4. a well-formed return whose pool is uninitialised reads zero, which is not depth.
    zeros = ["%064x" % 0] * 4
    assert decode_pool_state("0x" + head + "".join(zeros)) == (None, None)
    assert decode_pool_state("0x" + head + "".join([words[0], words[1], words[2], "%064x" % 0])) \
        == (None, None)
    # 5. a header that declares fewer than four words cannot answer, even when data follows.
    short_head = "%064x" % 32 + "%064x" % 2
    assert decode_pool_state("0x" + short_head + "".join(words[:2])) == (None, None)
    # 6. more words than asked for is fine: word 0 and word 3 are still the answer.
    long_head = "%064x" % 32 + "%064x" % 6
    assert decode_pool_state("0x" + long_head + "".join(words + words[:2])) == (sqrt_price, liquidity)
    # 7. garbage in the length field, and garbage in a word.
    assert decode_pool_state("0x" + "%064x" % 32 + "z" * 64 + "".join(words)) == (None, None)
    assert decode_pool_state("0x" + head + "z" * 64 + "".join(words[1:])) == (None, None)

    # 8. the calldata is one selector plus two 32-byte arguments, and it carries the slot0 key.
    slot0_key, liquidity_key = pool_state_slots("0x" + "ab" * 32)
    data = EXTSLOAD_MULTI + slot0_key + "%064x" % POOL_STATE_WORDS
    assert len(data) == 2 + 8 + 64 + 64, len(data)
    assert int(liquidity_key, 16) - int(slot0_key, 16) == 3, "liquidity is no longer at root+3"

    print("selftest ok — one extsload(bytes32,uint256) per pool, %d words, decode by length"
          % POOL_STATE_WORDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

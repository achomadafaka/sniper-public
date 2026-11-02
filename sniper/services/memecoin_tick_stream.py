"""Tick-stream ingest v0 — the quant data plane's first layer (observation ONLY).

Spec: docs/superpowers/specs/2026-07-23-memecoin-tick-stream-design.md. This worker records the
raw swap tape; it never reads for, nor feeds, any entry/exit decision. Fail-closed everywhere:
an RPC error skips the tick and resumes from the persisted cursor — the tape has gaps, never
inventions.

EVM lane: one `eth_getLogs` per chain per tick from the persisted cursor. 4663 is ingested
without an ADDRESS filter (whole-chain tape; busier chains like 8453 are restricted to watched
pools) — but ALWAYS filtered by TOPIC to `INGEST_TOPICS`. It is not "unfiltered": that wording
stood here while the topic list omitted V4, and reading it as unfiltered-full-stop is exactly how
the missing V4 tape went unnoticed. A topic absent from INGEST_TOPICS is never fetched, and its
absence in the tape is indistinguishable from a quiet chain. V2/V3 Swap logs are normalized to signed into-the-pool amounts;
V2 Sync rows carry reserves (LP-pull visibility).

Solana lane (separate entrypoint, asyncio): `logsSubscribe mentions=[mint]` per open Solana real
position — arrival events (slot, signature) only in v0; documented partial coverage.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

# The V4 native-currency sentinel. It is the quote asset, and address ordering puts it at
# currency0 on every native pool.
V4_NATIVE = "0x" + "00" * 20

from sniper.persistence import db_ticks as t
from sniper.execution import evm_v4_quote as V4Q
from sniper.services.memecoin_evm_v4_events import (
    decode_v4_log, decode_v4_initialize, V4_SWAP, V4_INITIALIZE)
from sniper.services.memecoin_evm_v3_events import V3_SWAP, PANCAKE_V3_SWAP, valid_pancake_v3_data

logger = logging.getLogger("memecoin")

V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V2_SYNC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"
# Solidly / Aerodrome pool: Swap(address indexed sender, address indexed to, uint256 amount0In,
# uint256 amount1In, uint256 amount0Out, uint256 amount1Out). The same four data words as a
# Uniswap-V2 Swap, so it decodes to the same `v2_swap` row (real frames from base-rpc.publicnode.com,
# tests/fixtures/memecoin/base_v2_swap_frames.json, 2026-09-28).
SOLIDLY_SWAP = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
# LP add/remove — the DIRECT liquidity-pull signal. Reserve direction alone cannot distinguish an
# LP exit from an ordinary buy (V2 emits Sync on every swap), which is why depth_pull_flag was
# permanently None until these were decoded. topic0 verified by keccak against the live Swap/Sync
# constants above. Mint(address,uint256,uint256) / Burn(address,uint256,uint256,address).
V2_MINT = "0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f"
V2_BURN = "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"
# WO-10 Uniswap-V3 depth. topic0 = keccak(canonical event signature) — self-checked against the live
# V3_SWAP/V2_SWAP constants above (test_memecoin_wo10_v3_depth). V3 fee claims are a distinct Collect
# event (never a Burn), so — unlike Solana, where fees masquerade as burns — a fee claim can never
# set depth-pull here. Mint/Burn carry tickLower/tickUpper as INDEXED int24 topics; Swap carries
# post-swap sqrtPriceX96 + in-range liquidity + current tick in its data words.
V3_MINT = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
V3_BURN = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"
V3_COLLECT = "0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0"
# PONS V2 (4663), the launchpad curve MRLN traded on while our tape summed $0.11 from a $2.51
# Uniswap pool and the gate refused a $259 floor (2026-09-23). A Pons v2 coin gets ONE curve
# contract of its own at launch, so the curve address is the pool id -- NOT the token address that
# GMGN reports as `pool_address`. The curve itself is unverified on the explorer; both topic0
# values are keccak of the signature below and are corroborated by openchain's signature database
# (hasVerifiedContract). The two trailing data words are protocol fees, exactly 1% and 2% of the
# quote leg on the measured launch-day buy, and are NOT part of the traded amount.
# CurveBuy/CurveSell(address indexed buyer, address indexed recipient,
#                    uint256 quoteAmount, uint256 tokenAmount, uint256 fee1, uint256 fee2)
PONS_V2_CURVE_BUY = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
PONS_V2_CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
# The launcher is a SINGLETON (0xe33e9e479df8802cb0866d5d05258bec4cf62948, verified as
# PonsV2LaunchAndBuy), so one address-filtered subscription names every curve on the chain.
# Slot layout read off MRLN's own launch (tx 0x6de2c273..., block 70509264) and pinned by test:
# Launched(address indexed token, address indexed curve, address indexed launcher,
#          address recipient, uint256 quoteIn, uint256 tokensOut)
PONS_V2_LAUNCHED = "0xdcacba5e347ae7abd91cb519eb877af8fa7774e347b85dd3ddcd24a2ba8cdf37"
PONS_V2_LAUNCHER = "0xe33e9e479df8802cb0866d5d05258bec4cf62948"
_INT256_SIGN = 1 << 255
_UINT256 = 1 << 256
_MAX_SPAN = 400          # cap one catch-up scan; a gap deeper than MAX_CATCHUP_BLOCKS is skipped
                         # to the tip (bounded 2026-07-23 — deep gaps were being ingest-stamped)


class TickRetryBackoff:
    def __init__(self, *, base_s, cap_s):
        self.base_s = max(0.0, float(base_s))
        self.cap_s = max(self.base_s, float(cap_s))
        self._failures = {}
        self._retry_at = {}

    def ready(self, chain_id, now):
        return float(now) >= self._retry_at.get(str(chain_id), 0.0)

    def record(self, chain_id, result, now):
        chain_id = str(chain_id)
        if result is not None:
            self._failures.pop(chain_id, None)
            self._retry_at.pop(chain_id, None)
            return 0.0
        failures = self._failures.get(chain_id, 0) + 1
        self._failures[chain_id] = failures
        delay = min(self.cap_s, self.base_s * (2 ** (failures - 1)))
        self._retry_at[chain_id] = float(now) + delay
        return delay


def chain_tick_period(chain_id, default_s, robinhood_s):
    """Use a slower period for the Robinhood whole-chain scan."""
    return max(float(default_s), float(robinhood_s)) if str(chain_id) == "4663" \
        else float(default_s)


def _s256(word_hex):
    v = int(word_hex, 16)
    return v - _UINT256 if v >= _INT256_SIGN else v


def _tick(topic):
    """A signed int24 packed into a 32-byte indexed topic (V3 tickLower/tickUpper)."""
    return _s256((topic or "0x")[2:] if str(topic).startswith("0x") else topic)


def _topic_addr(topic):
    return "0x" + topic[-40:]


# EVERY TOPIC THE DECODER CAN HANDLE, and the ONLY list the request may use.
#
# Until 2026-08-30 the request carried its own literal of eight V2/V3 topics while the decoder
# also handled V4 — so V4 logs were never FETCHED, the 4663 tape had ZERO V4 coverage across
# 11,864,229 rows, and the absence looked like a quiet chain. Our funded positions are on V4, so
# the tape had never seen the pools we actually trade.
#
# A GUARD CANNOT CATCH WHAT ITS OWN LIST OMITS. Keeping one list next to the decoder it must match
# is why this is a constant and not a literal at the call site; a second literal is how the two
# drift apart again. `tests/test_memecoin_tickstream_topic_coverage.py` DERIVES the decoder's
# vocabulary from the declared topic constants rather than restating it, so adding a decode branch
# without adding its topic here goes red.
INGEST_TOPICS = (
    V3_SWAP, PANCAKE_V3_SWAP, V2_SWAP, V2_SYNC, V2_MINT, V2_BURN, V3_MINT, V3_BURN, V3_COLLECT,
    # SOLIDLY_SWAP IS NOT HERE ON PURPOSE (2026-09-28, Beta T1): it is decoded for the volume tape
    # only. In this held-pool scan an Aerodrome pool would read as `v2` with swaps and no Sync (its
    # Sync(uint256,uint256) is another topic), changing exit and V6 inputs. See the topic-coverage
    # test's VOLUME_TAPE_ONLY.
    # V4 SWAPS ONLY (2026-08-30). The PoolManager is a singleton, so these arrive on the same
    # scan and cost no extra CALL -- but they nearly double the RESPONSE volume. Measured over
    # 500 blocks at a 0.10s block time (~855k blocks/day):
    #     current topic set        1,516 logs
    #     + V4_SWAP                2,767   (+83%)
    #     + V4_MODIFY_LIQUIDITY    2,927   (+93%)
    # on an endpoint that has already thrown 2,412 RPC 429s.
    #
    # V4_MODIFY_LIQUIDITY IS DELIBERATELY EXCLUDED even though it is only ~11% of the increase
    # and an LP pull is a form of liquidity death. Nothing reads it: `swap_lane_covers` and
    # `newest_swap_age_s` read SWAP events, on Solana too, so ingesting it today buys storage and
    # response volume for a column no consumer queries. Add it WITH the reader that needs it, and
    # re-measure then -- the cost is known and small, so this is deferral, not refusal.
    V4_SWAP,
    # Initialize: the ONLY mint->pool mapping a V4 pool has, and the reason pool_meta had ZERO V4
    # rows while the tape had 164,949 V4 swaps nothing could reach by mint. Rare -- 4 logs per 300
    # blocks measured -- so it rides the same scan for no meaningful volume.
    V4_INITIALIZE,
    # Pons v2 curve trades. The curves are per-token and unverifiable by address, so these ride a
    # topic-only filter exactly as V3 does.
    PONS_V2_CURVE_BUY,
    PONS_V2_CURVE_SELL,
)


def decode_pons_v2_launched(log):
    """One `Launched` log from the Pons v2 launcher -> (token, curve), or None.

    The curve address is our pool id. GMGN reports the TOKEN address as `pool_address` for the same
    coin, so the two identifiers disagree by design and must not be mixed.
    """
    topics = [str(t).lower() for t in (log.get("topics") or []) if t]
    if len(topics) < 3 or topics[0] != PONS_V2_LAUNCHED:
        return None
    if str(log.get("address") or "").lower() != PONS_V2_LAUNCHER:
        return None
    token, curve = _topic_addr(topics[1]), _topic_addr(topics[2])
    if len(token) != 42 or len(curve) != 42 or int(token, 16) == 0 or int(curve, 16) == 0:
        return None
    return token, curve


def decode_evm_log(chain_id, log, observed_at):
    """One raw eth_getLogs entry -> a normalized pool_ticks row, or None for foreign topics.
    Positive amounts flow INTO the pool (V2 in−out matches V3's sign convention)."""
    v4 = decode_v4_log(chain_id, log, observed_at)
    if v4 is not None:
        return v4
    topics = log.get("topics") or []
    if not topics:
        return None
    data = (log.get("data") or "0x")[2:]
    words = [data[i:i + 64] for i in range(0, len(data), 64)]
    base = {
        "chain_id": str(chain_id),
        "pool": str(log.get("address") or "").lower(),
        "block_no": int(log["blockNumber"], 16) if log.get("blockNumber") else None,
        "tx_sig": log.get("transactionHash"),
        "log_index": int(log["logIndex"], 16) if log.get("logIndex") is not None else None,
        "block_hash": log.get("blockHash"),           # WO-10: reorg/provenance anchor (WO-9 uses it)
        "observed_at": observed_at,
    }
    head = topics[0]
    try:
        if (head in (V3_SWAP, PANCAKE_V3_SWAP) and len(words) >= 2
                and (head != PANCAKE_V3_SWAP or valid_pancake_v3_data(log.get("data")))):
            # V3 Swap data words: amount0, amount1, sqrtPriceX96, liquidity(in-range), tick(current).
            # The in-range liquidity + tick are the depth series (the analog of V2 Sync reserves);
            # older captures with only the two amount words still decode (extra fields stay None).
            row = {**base, "kind": "v3_swap",
                   "maker": _topic_addr(topics[1]) if len(topics) > 1 else None,
                   "amount0": str(_s256(words[0])), "amount1": str(_s256(words[1]))}
            if len(words) >= 5:
                row.update(sqrt_price_x96=str(int(words[2], 16)),
                           liquidity=str(int(words[3], 16)), tick=_s256(words[4]))
            return row
        if head in (PONS_V2_CURVE_BUY, PONS_V2_CURVE_SELL) and len(words) >= 2:
            # SIGNS FOLLOW THE HOUSE RULE: positive flows INTO the pool. A buy sends the quote in
            # and the token out; a sell is the reverse. amount0 is the quote leg, recorded as
            # token0 only after background factory verification. The counter can be native or
            # ERC-20; its own decimals and USD price determine volume.
            # ⚠️ THE TWO EVENTS DO NOT SHARE A WORD ORDER, and assuming they did put token counts
            # in the quote leg of every sell for three minutes of live tape (2026-09-23 14:01Z):
            # sells read a p50 quote of 5,576,921 native units against a buy p50 of 0.039, so the
            # USD sum was inflated about 1e8. Each event puts its INPUT first.
            #   CurveBuy (measured, tx 0xd64a13bc...):  quoteIn  0.12849723, tokensOut 27,090,336.79
            #   CurveSell(measured, tx 0x7caf2ae1...):  tokensIn 4,234,177.86, quoteOut 0.02693422
            # Both fixtures are committed; a synthesised sell cannot prove this, and mine asserted
            # my own assumption back at me until the live tape contradicted it.
            buy = head == PONS_V2_CURVE_BUY
            first, second = int(words[0], 16), int(words[1], 16)
            quote, token = (first, second) if buy else (second, first)
            return {**base, "kind": "pons_v2_swap",
                    "maker": _topic_addr(topics[1]) if len(topics) > 1 else None,
                    "amount0": str(quote if buy else -quote),
                    "amount1": str(-token if buy else token)}
        # V3 LP add/remove — tickLower/tickUpper are INDEXED int24 topics; the uint128 liquidity
        # `amount` and the token amounts are data words. Burn removals read negative into-pool
        # (matching v2_burn), and `liquidity` carries the removed in-range liquidity the WO-3 severity
        # classifier grades against the latest v3_swap in-range liquidity.
        if head == V3_MINT and len(words) >= 4 and len(topics) >= 4:
            return {**base, "kind": "v3_mint",
                    "maker": _topic_addr(topics[1]),
                    "amount0": str(int(words[2], 16)), "amount1": str(int(words[3], 16)),
                    "liquidity": str(int(words[1], 16)),
                    "tick_lower": _tick(topics[2]), "tick_upper": _tick(topics[3])}
        if head == V3_BURN and len(words) >= 3 and len(topics) >= 4:
            return {**base, "kind": "v3_burn",
                    "maker": _topic_addr(topics[1]),
                    "amount0": str(-int(words[1], 16)), "amount1": str(-int(words[2], 16)),
                    "liquidity": str(int(words[0], 16)),
                    "tick_lower": _tick(topics[2]), "tick_upper": _tick(topics[3])}
        if head == V3_COLLECT and len(words) >= 3 and len(topics) >= 4:
            # Fee claim — carries NO liquidity and NO tick range, so it can never set depth-pull.
            return {**base, "kind": "v3_collect",
                    "maker": _topic_addr(topics[1]),
                    "amount0": str(int(words[1], 16)), "amount1": str(int(words[2], 16))}
        if head in (V2_SWAP, SOLIDLY_SWAP) and len(words) >= 4:
            a0 = int(words[0], 16) - int(words[2], 16)     # amount0In − amount0Out
            a1 = int(words[1], 16) - int(words[3], 16)
            return {**base, "kind": "v2_swap",
                    "maker": _topic_addr(topics[1]) if len(topics) > 1 else None,
                    "amount0": str(a0), "amount1": str(a1)}
        if head == V2_SYNC and len(words) >= 2:
            return {**base, "kind": "v2_sync", "maker": None,
                    "amount0": str(int(words[0], 16)), "amount1": str(int(words[1], 16))}
        # LP add/remove. Both carry (amount0, amount1) as the two non-indexed data words. Signed to
        # the pool_ticks convention — positive = INTO the pool — so a Burn reads negative, matching
        # how swaps are stored and making an LP exit unambiguous downstream.
        if head in (V2_MINT, V2_BURN) and len(words) >= 2:
            sign = 1 if head == V2_MINT else -1
            return {**base, "kind": "v2_mint" if head == V2_MINT else "v2_burn",
                    "maker": _topic_addr(topics[1]) if len(topics) > 1 else None,
                    "amount0": str(sign * int(words[0], 16)),
                    "amount1": str(sign * int(words[1], 16))}
    except (TypeError, ValueError):
        return None
    return None


def evm_tick_pass(tc, chain_id, rpc_call, now_iso, *, watched_pools=None):
    """One ingest tick for one EVM chain: scan (cursor, head] with a single eth_getLogs, insert,
    advance the cursor. `rpc_call(method, params)` returns the JSON-RPC `result` or raises.
    `watched_pools=None` means unfiltered (whole-chain tape). Returns inserted count or None on
    a skipped (failed) tick — the cursor only advances after a successful scan."""
    try:
        head = int(rpc_call("eth_blockNumber", []), 16)
    except Exception:
        logger.exception("tickstream %s: head read failed (tick skipped)", chain_id)
        return None
    last = t.get_cursor(tc, str(chain_id))
    if last is None:
        last = head - 1                                    # start at the tip; history is not invented
    if head <= last:
        return 0
    # Bound catch-up (2026-07-23 review P1): observed_at is stamped with ingest time, so replaying
    # a DEEP gap (e.g. after downtime) stamps hours-old swaps as "now" and poisons the feature
    # windows. A large gap is honest AS A GAP — skip forward to the tip and log it rather than
    # invent current-time history. Small gaps (< the bound) still slice normally.
    from sniper.settings.memecoin import MEMECOIN_TICKSTREAM_MAX_CATCHUP_BLOCKS as _MAX_CATCHUP
    if _MAX_CATCHUP > 0 and last < head - _MAX_CATCHUP:
        logger.warning("tickstream %s: cursor %d blocks behind head — skipping the gap and "
                       "resuming at the tip (observed_at is ingest-stamped; replaying deep "
                       "history would poison feature windows)", chain_id, head - last)
        t.record_gap(tc, str(chain_id), last + 1, head - 1, "deep_catchup_skipped", now_iso)
        t.set_cursor(tc, str(chain_id), head - 1, now_iso)
        return 0
    to_block = min(head, last + _MAX_SPAN)
    params = {"fromBlock": hex(last + 1), "toBlock": hex(to_block),
              "topics": [list(INGEST_TOPICS)]}
    scans = [params]
    if watched_pools is not None:
        watched = sorted({str(p).lower() for p in watched_pools if p})
        if not watched:
            t.set_cursor(tc, str(chain_id), to_block, now_iso)
            return 0
        # V4 pool IDs are keys inside the singleton, not RPC contract addresses.
        v4_ids = [pool for pool in watched if len(pool) == 66 and pool.startswith("0x")]
        contracts = [pool for pool in watched if pool not in v4_ids]
        scans = [dict(params, address=contracts)] if contracts else []
        if v4_ids:
            from sniper.execution.evm_v4_quote import V4
            manager = (V4.get(str(chain_id)) or {}).get("pool_manager")
            if not manager:
                logger.warning("tickstream %s: V4 manager unknown (tick skipped)", chain_id)
                return None
            scans.append(dict(params, address=[manager.lower()],
                              topics=[[V4_SWAP, V4_INITIALIZE], v4_ids]))
    try:
        logs = []
        for scan in scans:
            answer = rpc_call("eth_getLogs", [scan])
            if not isinstance(answer, list):
                raise ValueError(f"unexpected getLogs result: {type(answer).__name__}")
            logs.extend(answer)
    except Exception:
        logger.exception("tickstream %s: getLogs (%s..%s) failed (tick skipped)",
                         chain_id, last + 1, to_block)
        return None
    rows = [r for r in (decode_evm_log(chain_id, lg, now_iso) for lg in logs) if r]
    inserted = t.insert_ticks(tc, rows)
    # V4 mint->pool mappings ride the SAME scan. A V4 pool is a key inside the PoolManager
    # singleton rather than a contract, so pool_meta's token0()/token1() resolver structurally
    # cannot reach it -- it had ZERO V4 rows while the tape carried V4 swaps nothing could find
    # by mint. Fail-soft: a metadata write must never break tick ingest, and the tape is the
    # thing with a cursor to protect.
    try:
        upsert_v4_pool_meta(
            tc, [decode_v4_initialize(chain_id, lg, now_iso) for lg in logs])
    except Exception:
        logger.exception("tickstream %s: v4 pool_meta upsert failed (ticks unaffected)", chain_id)
    fill_v4_pool_key_cache(chain_id, logs)
    # FREE head timestamps (probed 2026-07-24): this node's eth_getLogs carries `blockTimestamp`
    # per log, so block_times fills at ingest with ZERO extra RPC. This is what makes event-time
    # features live at the head — per-block header enrichment (64/pass) can never keep up with a
    # ~10 blocks/s chain and is now backfill-only. Fail-closed: logs without the field add nothing.
    headers, conflicts = {}, set()
    for lg in logs:
        try:
            bn, ts = lg.get("blockNumber"), lg.get("blockTimestamp")
            if bn is not None and ts is not None:
                block = int(bn, 16)
                header = (int(ts, 16), str(lg["blockHash"]).lower() if lg.get("blockHash") else None)
                if block in headers and headers[block] != header:
                    conflicts.add(block)
                headers[block] = header
        except (TypeError, ValueError):
            continue
    headers = {b: value for b, value in headers.items() if b not in conflicts}
    if headers:
        t.backfill_block_times(tc, str(chain_id), list(headers), None, now_iso, header_fn=headers.get)
    t.set_cursor(tc, str(chain_id), to_block, now_iso)
    return inserted


def watched_base_pools(conn):
    """8453 watch set: pools we can currently attribute — the quote-route targets of open real
    Base positions' fills. Empty set -> the Base lane idles (cursor still advances)."""
    try:
        rows = conn.execute("""
            SELECT DISTINCT COALESCE(NULLIF(p.executed_pool, ''),
                NULLIF(json_extract(CASE WHEN json_valid(p.evm_route_json)
                    THEN p.evm_route_json ELSE '{}' END, '$.pool_id'), ''),
                NULLIF(json_extract(CASE WHEN json_valid(p.evm_route_json)
                    THEN p.evm_route_json ELSE '{}' END, '$.pool'), ''),
                f.quote_route_target) AS pool
            FROM memecoin_fills f JOIN memecoin_positions p ON p.id = f.position_id
            WHERE p.is_open = 1 AND p.execution_mode = 'real' AND p.network_id = '8453'
                AND (p.executed_pool IS NOT NULL OR p.evm_route_json IS NOT NULL
                    OR f.quote_route_target IS NOT NULL)
        """).fetchall()
        return {str(r[0]).lower() for r in rows if r[0]}
    except Exception:
        logger.exception("tickstream: base watch-set query failed (empty set this tick)")
        return set()


def open_solana_mints(conn):
    try:
        rows = conn.execute(
            "SELECT DISTINCT mint FROM memecoin_positions WHERE is_open = 1 "
            "AND execution_mode = 'real' AND (network_id = 'solana' OR network_id IS NULL)"
        ).fetchall()
        return {str(r[0]) for r in rows if r[0] and not str(r[0]).startswith("0x")}
    except Exception:
        logger.exception("tickstream: solana watch-set query failed (empty set this tick)")
        return set()


def parse_solana_ws_message(msg, sub_to_mint, observed_at):
    """One logsSubscribe frame -> ('ack', request_id, sub_id) | ('tick', row) | None.
    Pure — the ws loop stays a dumb transport."""
    if not isinstance(msg, dict):
        return None
    if "result" in msg and "id" in msg:
        return ("ack", msg["id"], msg["result"])
    params = msg.get("params") or {}
    result = (params.get("result") or {})
    value = result.get("value") or {}
    sig = value.get("signature")
    sub = params.get("subscription")
    mint = sub_to_mint.get(sub)
    if not sig or mint is None:
        return None
    row = {
        "chain_id": "solana", "pool": mint, "kind": "sol_tx",
        "block_no": (result.get("context") or {}).get("slot"),
        "tx_sig": sig, "log_index": 0, "maker": None,
        "amount0": None, "amount1": None, "observed_at": observed_at,
    }
    return ("tick", row)


def run_solana_arrival_lane(*, ws_url, ticks_conn_factory, mints_fn, now_fn,
                            refresh_s=60.0, sleep_fn=None, backoff_max_s=300.0,
                            on_session=None):
    """Blocking loop (run in its own thread): subscribe `mentions=[mint]` per open Solana real
    position, record arrival events. The connection is rebuilt every `refresh_s` so the watch set
    tracks position opens/closes; any error tears down and retries — gaps, never inventions.

    BACKOFF (2026-08-26). The retry was a flat 10s. When the endpoint's key was spent the lane
    logged 1,002 identical failures in a day, hammered a provider that had already said no, and
    buried the one line that mattered. The delay now doubles from 10s to `backoff_max_s` and resets
    the moment a session completes. A dead lane must get quieter and cheaper, not louder and more
    expensive — but it must never go SILENT, so every retry still logs, at WARNING once the delay
    has escalated past the first few attempts.

    DELIVERY IS COUNTED, NOT ASSUMED. `on_session(ticks, mints, error)` reports what each session
    actually WROTE. A subscription id is not delivery: `wss://solana-rpc.publicnode.com` returns a
    healthy id and delivers zero events forever (finding 2026-08-07), and the only way to tell that
    apart from a quiet market is to count rows.
    """
    import asyncio
    import json as _json
    import time as _time
    sleep_fn = sleep_fn or _time.sleep

    async def _session():
        import websockets
        written = 0
        mints = sorted(mints_fn())
        if not mints:
            return 0, 0
        async with websockets.connect(ws_url, open_timeout=10, ping_timeout=60) as ws:
            for i, mint in enumerate(mints, 1):
                await ws.send(_json.dumps({
                    "jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                    "params": [{"mentions": [mint]}, {"commitment": "confirmed"}]}))
            req_to_mint = {i: mint for i, mint in enumerate(mints, 1)}
            sub_to_mint = {}
            tc = ticks_conn_factory()
            try:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + float(refresh_s)
                while loop.time() < deadline:
                    timeout = max(0.1, deadline - loop.time())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout)
                    except asyncio.TimeoutError:
                        break
                    parsed = parse_solana_ws_message(
                        _json.loads(raw), sub_to_mint, now_fn())
                    if parsed is None:
                        continue
                    if parsed[0] == "ack":
                        _req, _sub = parsed[1], parsed[2]
                        if _req in req_to_mint:
                            sub_to_mint[_sub] = req_to_mint[_req]
                    elif parsed[0] == "tick":
                        t.insert_ticks(tc, [parsed[1]])
                        written += 1
            finally:
                tc.close()
        return written, len(mints)

    delay = 10.0
    attempt = 0
    while True:
        error = None
        ticks = watched = 0
        try:
            ticks, watched = asyncio.run(_session()) or (0, 0)
            delay, attempt = 10.0, 0          # a completed session clears the backoff
        except Exception as exc:
            error = exc
            attempt += 1
            # Always log; escalate the level rather than the rate. The first couple of failures are
            # ordinary reconnects, a sustained streak is a broken lane and should read that way.
            log = logger.exception if attempt <= 2 else logger.warning
            log("tickstream solana lane error attempt=%d next_retry_s=%.0f (%s)",
                attempt, delay, type(exc).__name__)
            delay = min(float(backoff_max_s), delay * 2.0)
        if on_session is not None:
            try:
                on_session(ticks, watched, error)
            except Exception:
                logger.exception("tickstream solana lane on_session hook failed")
        sleep_fn(delay)


def fill_v4_pool_key_cache(chain_id, logs):
    """Store the full PoolKey of every V4 pool this scan just saw born.

    `pool_meta` (above) keeps the two CURRENCIES, because that is the mint -> pool direction
    discovery asks in. The quoter needs three more fields -- fee, tickSpacing and hooks -- and they
    are packed words in the SAME `Initialize` log we already hold. Until now nothing read them, so
    an entry on a first-touch coin paid a wide, unreliable chain read for bytes that had already
    crossed this process.

    Deliberately NOT written into `pool_meta`: that table lives in the research ticks DB, and the
    funded quote path must not take a dependency on a research file. One cache
    (`memecoin_evm_v4_pool_keys`), two fillers -- this live lane and the offline backfill indexer.

    Fail-soft like its sibling: the tape has a cursor to protect and a cache is an optimisation.
    """
    try:
        V4Q.store_pool_keys_from_logs(chain_id, logs)
        # WARM THE POOL-KEY FAMILY RANKING HERE, NOT ON THE ENTRY PATH (2026-09-06). Deriving a key
        # from a provider's pair needs the ranked families, and building that ranking is a full
        # GROUP BY over the key table -- 340 ms at 165k rows. The funded entry quote runs at a p50
        # of 545 ms on 4663, so paying it there would be a 62% add on the one path that races other
        # buyers. This pass already walks these logs, runs per chain, and is nowhere near the money.
        # Behind the same TTL, so it is a dict check on all but one pass in ten minutes.
        V4Q.warm_pool_key_families(chain_id)
    except Exception:
        logger.exception("tickstream %s: v4 pool key cache fill failed (ticks unaffected)", chain_id)


def upsert_v4_pool_meta(conn, rows):
    """Write V4 mint->pool mappings decoded from Initialize. Returns rows written.

    Idempotent: the same Initialize log re-ingested after a cursor rewind must not error, and the
    mapping is IMMUTABLE (a pool's currencies never change), so an existing row is left alone
    rather than restamped.
    """
    rows = [r for r in (rows or []) if r]
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO pool_meta (chain_id, pool, token0, token1, resolved_at) "
        "VALUES (?,?,?,?,?)",
        [(r["chain_id"], r["pool"], r["token0"], r["token1"], r.get("resolved_at"))
         for r in rows])
    conn.commit()
    return len(rows)


def backfill_v4_pool_meta(conn, chain_id, pool_ids, rpc_url, key_fn=None, now_iso=None,
                          budget=None, quote_tokens=None, dec_fn=None):
    """Resolve V4 pools that ALREADY initialized into rows the canonical resolver can USE.

    The forward resolver only fills from the moment it deploys. A V4 pool emits Initialize once,
    so a pool born earlier never re-announces itself and stays unresolvable for ever.

    This is a TARGETED lookup, not a block sweep. The pool id is an INDEXED topic, so one
    address-filtered eth_getLogs answers for ONE pool over all history, and the cost is bounded by
    the size of our universe rather than by chain history.

    USABLE, NOT MERELY PRESENT. The one consumer (memecoin_exits.py:1143) requires
    `quote_side IS NOT NULL AND token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL`, and
    discards any row that is not. Measured 2026-08-30: 59,869 of 60,881 42-char rows were usable
    (98.3%) and 0 of 214 V4 rows were (0%), because a mapping written from Initialize carries
    token0/token1 alone. Row count read as coverage while the witness stayed blind on every V4
    pool — including the funded Universal Router route we used the same day. So this writes
    orientation and decimals too, and the caller must report the USABLE count.

    The 140-of-214 majority case is a pool whose currency0 is the native sentinel. Native is the
    quote asset, it sorts first so it is always currency0, and it has no contract to call, so its
    decimals are 18 by definition. `tick_features` cannot rescue those rows: it calls token0() on
    the pool CONTRACT, which a V4 pool is not, and it treats a NULL quote_side as done.

    An existing row is ENRICHED in place, never rewritten: the mapping is immutable, so
    `resolved_at` keeps its original stamp and a field that is already set is left alone.

    Orientation and scale are never guessed. An unknown quote token leaves quote_side NULL and a
    failed decimals call leaves that column NULL, because a wrong orientation inverts price and a
    defaulted scale is off by a power of ten. The row stays unusable, which is the honest state.
    """
    from sniper.execution.evm_v4_quote import _pool_key
    key_fn = key_fn or (lambda ch, pid, rpc: _pool_key(ch, pid, rpc))
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    quotes = {str(q).lower() for q in (quote_tokens or ())} | {V4_NATIVE}
    dec_fn = dec_fn or (lambda tok: None)
    dec_cache = {V4_NATIVE: 18}          # native has no contract; 18 by definition, never a call

    def _dec(tok):
        tok = str(tok).lower()
        if tok not in dec_cache:
            dec_cache[tok] = dec_fn(tok)
        return dec_cache[tok]

    def _side(t0, t1):
        if str(t0).lower() in quotes:
            return 0
        if str(t1).lower() in quotes:
            return 1
        return None                      # unknown quote token -> unusable, never guessed

    rep = {"requested": 0, "already": 0, "skipped_shape": 0, "resolved": 0, "enriched": 0,
           "unresolved": 0, "written": 0, "unresolved_pools": []}
    seen = set()
    for raw in (pool_ids or []):
        pid = str(raw or "").lower()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        rep["requested"] += 1
        # Same shape guard as the forward path: a 42-char id is a V2/V3 pool ADDRESS, and asking
        # the PoolManager about it returns nothing for ever.
        if len(pid) != 66:
            rep["skipped_shape"] += 1
            continue
        row = conn.execute(
            "SELECT token0, token1, quote_side, token0_decimals, token1_decimals FROM pool_meta "
            "WHERE chain_id = ? AND pool = ?", (str(chain_id), pid)).fetchone()
        if row is not None:
            if (row["quote_side"] is not None and row["token0_decimals"] is not None
                    and row["token1_decimals"] is not None):
                rep["already"] += 1      # usable already; never re-fetch, never restamp
                continue
            # The mapping is present but unusable. Enrich from the STORED currencies — no
            # Initialize lookup is needed, so this costs at most two decimals calls.
            t0, t1 = str(row["token0"]).lower(), str(row["token1"]).lower()
            sets, args = [], []
            if row["quote_side"] is None and _side(t0, t1) is not None:
                sets.append("quote_side = ?"); args.append(_side(t0, t1))
            for col, tok in (("token0_decimals", t0), ("token1_decimals", t1)):
                if row[col] is None and _dec(tok) is not None:
                    sets.append(f"{col} = ?"); args.append(_dec(tok))
            if sets:
                conn.execute(f"UPDATE pool_meta SET {', '.join(sets)} "
                             "WHERE chain_id = ? AND pool = ?", (*args, str(chain_id), pid))
                conn.commit()
                rep["enriched"] += 1
            continue
        if budget is not None and rep["resolved"] + rep["unresolved"] >= budget:
            break
        try:
            key = key_fn(str(chain_id), pid, rpc_url)
        except Exception:
            logger.exception("v4 backfill %s: pool key lookup raised for %s", chain_id, pid)
            key = None
        if not key or not key.get("currency0") or not key.get("currency1"):
            rep["unresolved"] += 1
            rep["unresolved_pools"].append(pid)
            continue
        t0, t1 = str(key["currency0"]).lower(), str(key["currency1"]).lower()
        rep["resolved"] += 1
        rep["written"] += upsert_v4_pool_meta(conn, [{
            "chain_id": str(chain_id), "pool": pid, "token0": t0, "token1": t1,
            "resolved_at": now_iso}])
        conn.execute("UPDATE pool_meta SET quote_side = ?, token0_decimals = ?, "
                     "token1_decimals = ? WHERE chain_id = ? AND pool = ?",
                     (_side(t0, t1), _dec(t0), _dec(t1), str(chain_id), pid))
        conn.commit()
    return rep

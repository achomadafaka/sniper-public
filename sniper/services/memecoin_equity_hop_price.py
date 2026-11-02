"""Price a pool's quote unit through ONE hop into a unit the volume feed already prices.

WHY (2026-09-22, GIGABYTE ref 51a1a1b6 and the equity map in
docs/audits/2026-09-22-4663-equity-quote-price-map.md). On Robinhood 4663 about 17,000 pools are
quoted in tokenized equities (NVDA, META, HOOD, SPCX, AMD ...) and in a few non-equity units (AI,
FLX, ROBIN, musebook). The on-chain m5 feed prices only the native coin, WETH and the configured
stables, so every swap in those pools was UNPRICEABLE and the m5 gate fell back to a provider that
undercounts. Each of those units has a live V4 pool into ETH, WETH or USDG. One hop through that
pool, read from the PoolManager's own storage, prices the unit in a currency the feed already
converts. It is the only honest source for the private-company and leveraged wrappers, which have
no public quote at all.

RULES:
  1. NEVER ON THE ENTRY PATH. `read_for_gate` reads only the local volume database. This module
     runs inside the volume observer, on its own cadence, and writes `volume_quote_prices` and
     `pool_meta` for the gate to read later. A gate read pays no network call for it.
  2. UNKNOWN IS NEVER ZERO. A unit with no hop pool above the depth floor, no readable state, no
     decimals or a stale base price gets NO price row. The pools quoted in it stay unpriced, which
     the gate already reports as undecidable.
  3. A price carries its age. `volume_quote_prices.observed` is the read time; the gate refuses a
     row older than its own limit, so a stalled refresh cannot serve yesterday's price.
  4. Bounded reads. One refresh reads at most MAX_READS storage slots on the observer's RPC.

ponytail: the deepest hop pool by in-range liquidity is the price; a leveraged wrapper is priced
as the token it is, never as its underlying. Add a second-source cross-check if a thin hop is ever
seen to move the gate.
"""
from __future__ import annotations

import logging
import math
import os

from sniper.execution.evm_v4_multihop import _EXTSLOAD, _LIQUIDITY_OFFSET, _POOLS_SLOT

logger = logging.getLogger(__name__)

NATIVE = "0x" + "00" * 20
DECIMALS_CALL = "0x313ce567"
BASE_MAX_AGE_S = 60.0          # the base unit's own price must be as fresh as the gate requires
MIN_POOLS = int(os.getenv("MEMECOIN_ONCHAIN_M5_HOP_MIN_POOLS", "25"))
MAX_UNITS = int(os.getenv("MEMECOIN_ONCHAIN_M5_HOP_MAX_UNITS", "32"))   # 16 -> 32 on 2026-09-22: GIGABYTE's unit ranked 17th
MAX_HOP_POOLS = int(os.getenv("MEMECOIN_ONCHAIN_M5_HOP_MAX_POOLS_PER_UNIT", "4"))
MAX_READS = int(os.getenv("MEMECOIN_ONCHAIN_M5_HOP_MAX_READS", "160"))   # 80 -> 160 on 2026-09-22; the pass runs on its own thread
# Depth floor in PoolManager liquidity units. Gamma's 2026-09-22 read: 13 of 15 units sit above
# 1e19; HOOD's best hop is 1.67e17 and FLX's 4.0e14, thin enough to move with one trade.
# A FLOOR IN DOLLARS, NOT IN RAW LIQUIDITY (2026-09-22). `liquidity` is sqrt(x*y) in ATOMS, so one
# raw floor meant ~1 ETH x 1 unit on an 18/18 pool and ~1e6 x that on a 6-decimal USDG pool: every
# HOOD/USDG hop read "no_hop_above_floor" while GIGABYTE's 60 swaps in the window went unpriced.
MIN_DEPTH_USD = float(os.getenv("MEMECOIN_ONCHAIN_M5_HOP_MIN_DEPTH_USD", "1000"))
RECENT_SWAPS_S = float(os.getenv("MEMECOIN_ONCHAIN_M5_HOP_RECENT_SWAPS_S", "600"))


def iso(at):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(at), tz=timezone.utc).isoformat()


def depth_usd(liquidity, sqrt_price_x96, *, base_is_token0, base_decimals, base_usd):
    """The base-side amount a V4 position of `liquidity` holds around the current price, in USD.

    x = L / sqrtP for token0, y = L * sqrtP for token1 (atoms, at the current tick); it is the
    order of magnitude of what a swap can take before the price moves a lot, which is what a hop
    price needs to be worth trusting. Decimal-aware, so a 6-decimal stable and an 18-decimal ETH
    pool measure on the same scale. It is the VIRTUAL reserve of the current tick, not TVL
    (Codex review 2026-09-23, F6): a narrow concentrated position reads far deeper than it is,
    which is why the trust rule also requires recent swaps (RECENT_SWAPS_S) and never this
    number alone.
    """
    try:
        sp = float(sqrt_price_x96) / float(1 << 96)
        if sp <= 0 or float(liquidity) <= 0:
            return 0.0
        atoms = float(liquidity) / sp if base_is_token0 else float(liquidity) * sp
        value = atoms / (10 ** int(base_decimals)) * float(base_usd)
        return value if math.isfinite(value) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _rpc_call(rpc_url, to, data, http=None):
    from sniper.execution.evm_read_rpc import _rpc
    response = _rpc(rpc_url, "eth_call", [{"to": to, "data": data}, "latest"], http=http)
    raw = response.get("result") if isinstance(response, dict) else None
    return raw if isinstance(raw, str) and raw.startswith("0x") and raw != "0x" else None


def _state_slot(pool_id):
    from eth_utils import keccak
    return int.from_bytes(
        keccak(bytes.fromhex(str(pool_id)[2:]) + _POOLS_SLOT.to_bytes(32, "big")), "big")


def read_pool_state(chain, pool_id, rpc_url, *, http=None, rpc_call=_rpc_call):
    """(sqrt_price_x96, liquidity) of one V4 pool from PoolManager storage, or None for either."""
    from sniper.execution.evm_v4_quote import V4
    manager = V4[str(chain)]["pool_manager"]
    base = _state_slot(pool_id)
    out = []
    for offset, mask in ((0, (1 << 160) - 1), (_LIQUIDITY_OFFSET, (1 << 128) - 1)):
        slot = (base + offset).to_bytes(32, "big").hex()
        try:
            raw = rpc_call(rpc_url, manager, "0x" + _EXTSLOAD + slot, http=http)
            out.append(int(raw, 16) & mask if raw else None)
        except Exception:
            out.append(None)
    return out[0], out[1]


def unit_price_in_base(sqrt_price_x96, *, unit_is_token0, unit_decimals, base_decimals):
    """How much BASE one whole UNIT buys at this sqrt price, or None when the price is degenerate."""
    if not sqrt_price_x96 or sqrt_price_x96 <= 0:
        return None
    ratio = (sqrt_price_x96 / (1 << 96)) ** 2        # token1 per token0, in raw atoms
    if unit_is_token0:
        price = ratio * 10 ** (unit_decimals - base_decimals)
    else:
        price = (1.0 / ratio) * 10 ** (unit_decimals - base_decimals)
    return price if math.isfinite(price) and price > 0 else None


def base_prices(conn, chain, now, *, max_age_s=BASE_MAX_AGE_S):
    """Priced base units on `chain` with a fresh row: {token: usd}."""
    rows = conn.execute(
        "SELECT token, usd, observed FROM volume_quote_prices WHERE chain_id=?", (str(chain),))
    out = {}
    for token, usd, observed in rows:
        if usd and observed is not None and 0 <= now - float(observed) <= max_age_s:
            out[str(token).lower()] = float(usd)
    return out


def anchor_tokens(chain):
    """The tokens a hop may be priced AGAINST: native, the chain's quote token, its USD stables.

    ONE HOP, FROM AN ANCHOR (2026-09-22). With every priced token admitted as a hop base, a unit's
    deepest-looking pool was one against a coin priced at $0.0002 through a thin pool of its own,
    so GIGABYTE's quote unit read $5.8e14 through that pool and was skipped while its real ETH pool
    ($24k of depth, $652) sat one line below. A base that is itself a hop price compounds its error
    and has no depth guarantee; the anchors have both.
    """
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.settings import memecoin as cfg
    out = {NATIVE, CHAIN_QUOTE[str(chain)]["token"].lower()}
    out |= {t.lower() for t in cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get(str(chain), ())}
    return out


def candidate_units(conn, chain, *, exclude, min_pools=MIN_POOLS, max_units=MAX_UNITS, now=None,
                    recent_s=RECENT_SWAPS_S):
    """Unpriced tokens that quote unresolved pools, the ones being TRADED first.

    Ranked by swaps in their unresolved pools over the last `recent_s` seconds, then by pool count.
    Pool count alone (the 09-22 first cut) put the equity units with hundreds of idle pools in
    every one of the 16 slots, and the unit GIGABYTE's 171-swap pool quotes in never got a turn.
    A unit with fewer than `min_pools` pools still qualifies when one of them traded recently.
    """
    since = iso(float(now) - float(recent_s)) if now is not None else None
    rows = conn.execute(
        "SELECT t, COUNT(*) n, SUM(s) swaps FROM ("
        "  SELECT m.token0 t, (SELECT COUNT(*) FROM pool_ticks k WHERE k.chain_id=m.chain_id AND k.pool=m.pool"
        "                     AND (? IS NULL OR k.observed_at>=?)) s"
        "  FROM pool_meta m WHERE m.chain_id=? AND m.quote_side IS NULL"
        "  UNION ALL"
        "  SELECT m.token1, (SELECT COUNT(*) FROM pool_ticks k WHERE k.chain_id=m.chain_id AND k.pool=m.pool"
        "                     AND (? IS NULL OR k.observed_at>=?))"
        "  FROM pool_meta m WHERE m.chain_id=? AND m.quote_side IS NULL)"
        " GROUP BY t HAVING n >= ? OR swaps > 0 ORDER BY swaps DESC, n DESC",
        (since, since, str(chain), since, since, str(chain), int(min_pools)))
    out = []
    for token, _n, _swaps in rows:
        token = str(token).lower()
        if token not in exclude:
            out.append(token)
        if len(out) >= max_units:
            break
    return out


TAPE_MAX_AGE_S = float(os.getenv("MEMECOIN_ONCHAIN_M5_HOP_TAPE_MAX_AGE_S", "3600"))


def hop_pools(conn, chain, unit, bases):
    """Known pools pairing `unit` with a priced base: [(pool_id, base_token, unit_is_token0)].

    THIS WAS THE HOT CORE (2026-09-23 23:00Z, operator: "find what's throttling the pc"). The
    old query was `token0=? OR token1=?`, which SQLite can only serve by walking every pool_meta
    row of the chain (90,128 on 4663), and the filter against `bases` and the 66-character V4 id
    ran in Python over the whole result: the zero-address unit alone returned 36,231 rows. With
    168 units per refresh that was 19.9 s of CPU every 30-second pass, one core pegged, package
    at 97 C and the laptop thermal-throttling everything else, the funded runner included. Two
    indexed seeks (pool_meta_token0, pool_meta_token1) with the base set and the id length in the
    WHERE clause return only the pairs that matter: 4,564 executes and 12.5 s become a few ms.
    pool_meta stores lowercase addresses (0 mixed-case rows on 4663), so the comparison is exact.
    """
    base_list = sorted({str(b).lower() for b in bases if b})
    unit = str(unit).lower()
    if not base_list or len(base_list) > 900:                 # SQLite bind limit, never hit today
        return _hop_pools_scan(conn, chain, unit, set(base_list))
    marks = ",".join("?" * len(base_list))
    rows = conn.execute(
        f"SELECT pool, token1, 1 FROM pool_meta WHERE chain_id=? AND token0=? "
        f"AND token1 IN ({marks}) AND length(pool)=66 "
        f"UNION ALL "
        f"SELECT pool, token0, 0 FROM pool_meta WHERE chain_id=? AND token1=? "
        f"AND token0 IN ({marks}) AND length(pool)=66",
        (str(chain), unit, *base_list, str(chain), unit, *base_list))
    return [(str(pool).lower(), str(other).lower(), bool(is_t0)) for pool, other, is_t0 in rows]


def _hop_pools_scan(conn, chain, unit, bases):
    """The pre-2026-09-23 shape, kept only for the bind-limit fallback and as the test oracle."""
    rows = conn.execute(
        "SELECT pool, token0, token1 FROM pool_meta WHERE chain_id=? AND (token0=? OR token1=?)",
        (str(chain), unit, unit))
    out = []
    for pool, t0, t1 in rows:
        t0, t1 = str(t0).lower(), str(t1).lower()
        other = t1 if t0 == unit else t0
        if other in bases and len(str(pool)) == 66:          # V4 pool ids only
            out.append((str(pool).lower(), other, t0 == unit))
    return out


def tape_states(conn, chain, pools, now, *, max_age_s=TAPE_MAX_AGE_S):
    """{pool_id: (sqrt_price_x96, liquidity, evidence_ts)} from each pool's LATEST VERIFIED swap.

    A V4 Swap event carries the pool's sqrtPriceX96 and in-range liquidity after the swap, so an
    active hop pool is priced from the tape with no chain read at all. SPCX's most traded hop
    (168 swaps in an hour) held 3.4e17 liquidity while a quieter one held 2.8e21, so the deepest
    pool, not the busiest, is the price, and both facts come from the same rows.

    TWO THINGS THE ROW CARRIES THAT THIS USED TO THROW AWAY (F4, 2026-09-23).

    ITS IDENTITY. A tick from a block the chain later replaced is not the pool's state, and since
    F1 that is decidable: the tick's `block_hash` against the canonical one in `block_times`. A
    MISMATCH is an orphan, a decided exclusion. A NULL on either side cannot be judged at all and
    is excluded as unverified. Neither is ever treated as "no state", which would read as a silent
    pool and spend a chain read.
    ⚠️ PREVENTIVE TODAY, and the commit says so rather than implying a live loss: of 118,241
    hop-eligible ticks on 4663 in the hour measured on 2026-09-23, 0 carried a NULL tick hash, 0 a
    NULL header hash and 0 mismatched.

    ITS TIME, and this one is a live defect. The state is selected within `TAPE_MAX_AGE_S`, which
    is 3600, and the gate accepts the derived price for `MEMECOIN_ONCHAIN_M5_HOP_PRICE_MAX_AGE_S`,
    which is 180. Measured on 4663 over 3,267 hop-eligible pools: the newest state per pool is p50
    817 s, p90 2,842 s, max 3,598 s old, so only 20.5% are inside the 180 s the gate believes it is
    enforcing. The evidence time returned here is the BLOCK's own clock (`block_times.block_ts`),
    never `observed_at`, which is when WE ingested the frame.

    Returns (states, stats) where stats counts `orphan`, `unverified` and `used`.
    """
    from datetime import datetime, timezone
    since = datetime.fromtimestamp(now - max_age_s, tz=timezone.utc).isoformat()
    out, stats = {}, {"orphan": 0, "unverified": 0, "used": 0}
    for pool_id in pools:
        row = conn.execute(
            "SELECT t.sqrt_price_x96, t.liquidity, t.block_hash, b.block_hash, b.block_ts "
            "FROM pool_ticks t LEFT JOIN block_times b "
            "  ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
            "WHERE t.chain_id=? AND t.pool=? AND t.observed_at>=? "
            "AND t.sqrt_price_x96 IS NOT NULL AND t.liquidity IS NOT NULL "
            "ORDER BY t.observed_at DESC LIMIT 1", (str(chain), pool_id, since)).fetchone()
        if row is None:
            continue
        tick_hash, header_hash, block_ts = row[2], row[3], row[4]
        if tick_hash is None or header_hash is None:
            stats["unverified"] += 1
            continue
        if str(tick_hash).lower() != str(header_hash).lower():
            stats["orphan"] += 1
            continue
        try:
            evidence_ts = datetime.fromisoformat(
                str(block_ts).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            stats["unverified"] += 1          # a verified identity with no readable time is still
            continue                          # an age we cannot state, and an unstated age is the defect
        try:
            out[pool_id] = (int(row[0]), int(row[1]), float(evidence_ts))
        except (TypeError, ValueError):
            continue
        stats["used"] += 1
    return out, stats


def base_price_times(conn, chain):
    """{token: observed} for the base units, so a derived price can carry its OLDEST evidence."""
    return {str(token).lower(): float(observed)
            for token, observed in conn.execute(
                "SELECT token, observed FROM volume_quote_prices "
                "WHERE chain_id=? AND usd IS NOT NULL AND observed IS NOT NULL", (str(chain),))}


def _decimals(conn, chain, token, rpc_url, now, *, http=None, rpc_call=_rpc_call):
    row = conn.execute("SELECT decimals FROM volume_token_decimals WHERE chain_id=? AND token=?",
                       (str(chain), token)).fetchone()
    if row is not None and row[0] is not None:
        return int(row[0]), 0
    try:
        raw = rpc_call(rpc_url, token, DECIMALS_CALL, http=http)
        value = int(raw, 16) if raw else None
    except Exception:
        value = None
    if type(value) is not int or not 0 <= value <= 255:
        return None, 1
    conn.execute("INSERT OR REPLACE INTO volume_token_decimals VALUES (?,?,?,?)",
                 (str(chain), token, value, now))
    conn.commit()                       # never carry a write into the next chain read
    return value, 1


def resolve_quote_sides(conn, chain, unit, decimals, now, *, bases=()):
    """Set quote_side and decimals on unresolved pools whose OTHER side is not a known unit.

    Only pools with quote_side NULL are touched, and only when exactly one side is `unit` and the
    other side is not itself a priced base (a unit/ETH hop pool is quoted in ETH, and stays for
    the observer to decide). Returns the number of rows set.

    `now` is kept for the call signature and is deliberately UNUSED: this pass no longer writes
    `resolved_at` (see the comment on the UPDATE below).
    """
    # TWO INDEXED SEEKS, NOT AN OR SCAN (2026-09-23 23:00Z, the same hot core as hop_pools): the
    # `token0=? OR token1=?` shape walked every pool_meta row of the chain for each of ~130 units
    # per refresh, 9.1 s of CPU a pass. Each half seeks its own index and returns only the side
    # that is unresolved. pool_meta is lowercase throughout, so the comparison is exact.
    unit = str(unit).lower()
    rows = conn.execute(
        "SELECT pool, token1, 0 FROM pool_meta WHERE chain_id=? AND token0=? AND quote_side IS NULL "
        "UNION ALL "
        "SELECT pool, token0, 1 FROM pool_meta WHERE chain_id=? AND token1=? AND quote_side IS NULL",
        (str(chain), unit, str(chain), unit)).fetchall()
    n = 0
    for pool, other, side in rows:
        other = str(other).lower()
        if other == unit or other in bases:
            continue
        column = "token0_decimals" if side == 0 else "token1_decimals"
        # REGISTRATION TIME IS NOT THIS PASS'S TO REWRITE (2026-09-24). This UPDATE used to set
        # `resolved_at=?` from the caller's `now`, which is an epoch FLOAT here
        # (`memecoin_volume_writer._start_hop_refresher` passes `real_utc_now()`), while every
        # other writer of the column stores `iso(now)`. Both readers of `pool_meta.resolved_at`
        # compare it as a STRING, so a restamped row sorts below every ISO stamp ('1' < '2'):
        # `memecoin_v4_key_seed` (`resolved_at > ?`) then never sees that pool again, and the
        # writer's own retry window (`resolved_at <= ?`) reads it as permanently expired. It had
        # stamped 16,350 rows, 11 of them V4 rows carrying a fee the key seeder could have used.
        # Nothing here wants a fresh stamp: this pass resolves a side, it does not register a pool.
        conn.execute("UPDATE pool_meta SET quote_side=?, %s=? WHERE chain_id=? AND pool=?"
                     % column, (side, int(decimals), str(chain), pool))
        n += 1
    return n


def refresh(conn, chain, rpc_url, now, *, http=None, rpc_call=_rpc_call,
            min_depth_usd=MIN_DEPTH_USD, max_reads=MAX_READS, max_units=MAX_UNITS):
    """One bounded pass: price every candidate unit through its deepest hop and write the rows.

    Returns a summary: {"priced": {unit: usd}, "skipped": {unit: reason}, "reads": n, "resolved": n}.
    Commits once at the end. Never raises on a chain read; a failed unit is skipped and named.
    """
    chain = str(chain)
    priced = base_prices(conn, chain, now)
    base_times = base_price_times(conn, chain)
    anchors = anchor_tokens(chain)
    bases = {t: p for t, p in priced.items() if t in anchors}       # hops price against anchors only
    summary = {"priced": {}, "skipped": {}, "reads": 0, "resolved": 0}
    if not bases:
        summary["skipped"]["*"] = "no_fresh_base_price"
        return summary
    known_units = {t for t, in conn.execute(
        "SELECT token FROM volume_quote_prices WHERE chain_id=? AND usd IS NOT NULL AND observed>=?",
        (chain, now - 600))}
    # ALREADY-PRICED UNITS FIRST, AND ALL OF THEM (2026-09-22). They price from the tape at zero
    # reads, and the gate only honours a row younger than its freshness window; on a pass that
    # spends its read budget on new candidates first, a known unit at the end of the list went
    # stale mid-pass. They do NOT count against `max_units`: with 18 known units and 16 slots the
    # candidates never ran at all, and HOOD (981 unresolved pools, 69 swaps in 10 min) sat at the
    # top of the candidate list for hours without a turn.
    units = sorted(known_units - set(bases) - {NATIVE})
    units += candidate_units(conn, chain, exclude=set(bases) | {NATIVE} | set(units),
                             max_units=max_units, now=now)
    units = [u for u in units if u not in anchors]
    # TAPE-PRICEABLE UNITS BEFORE UNITS THAT NEED THE CHAIN (2026-09-22). A unit whose hop pool
    # swapped inside the tape window costs zero storage reads; one whose hop pools are silent costs
    # two per pool. Spending the read budget in rank order starved the zero-read units behind it
    # (GIGABYTE's and PATIENCE's units ranked 16th and 20th and got `read_budget`).
    taped_pools = {r[0] for r in conn.execute(
        "SELECT DISTINCT pool FROM pool_ticks WHERE chain_id=? AND observed_at>=? AND sqrt_price_x96 IS NOT NULL",
        (chain, iso(float(now) - TAPE_MAX_AGE_S)))}
    pools_of = {unit: hop_pools(conn, chain, unit, bases) for unit in units}     # one lookup per unit
    def _has_taped_hop(unit):
        return any(pool in taped_pools for pool, _b, _o in pools_of[unit])
    units = [u for u in units if _has_taped_hop(u)] + [u for u in units if not _has_taped_hop(u)]
    for unit in units:
        if summary["reads"] >= max_reads:
            summary["skipped"][unit] = "read_budget"
            continue
        pools = pools_of[unit]
        if not pools:
            summary["skipped"][unit] = "no_hop_pool"
            continue
        decimals, spent = _decimals(conn, chain, unit, rpc_url, now, http=http, rpc_call=rpc_call)
        summary["reads"] += spent
        if decimals is None:
            summary["skipped"][unit] = "decimals_unreadable"
            continue
        best = None

        def _depth(base_token, unit_is_token0, sqrt_price, liquidity):
            base_dec = 18 if base_token == NATIVE else _decimals(
                conn, chain, base_token, rpc_url, now, http=http, rpc_call=rpc_call)[0]
            if base_dec is None or not liquidity or not sqrt_price:
                return 0.0, base_dec
            return depth_usd(liquidity, sqrt_price, base_is_token0=not unit_is_token0,
                             base_decimals=base_dec, base_usd=bases[base_token]), base_dec
        # THE TAPE FIRST, THE CHAIN ONLY FOR SILENT POOLS. Every hop pool with a swap in the window
        # is priced from its own latest Swap event, zero reads; storage is read only when no taped
        # pool clears the floor, and then only for a bounded few. The floor is dollars of base-side
        # depth (`depth_usd`), never raw liquidity.
        taped, tape_stats = tape_states(conn, chain, [p[0] for p in pools], now)
        for key in ("orphan", "unverified"):
            summary.setdefault("tape_excluded", {}).setdefault(key, 0)
            summary["tape_excluded"][key] += tape_stats[key]
        for pool_id, base_token, unit_is_token0 in pools:
            state = taped.get(pool_id)
            if not state:
                continue
            sqrt_price, liquidity, evidence_ts = state
            depth, base_dec = _depth(base_token, unit_is_token0, sqrt_price, liquidity)
            if depth < min_depth_usd:
                continue
            if best is None or depth > best[0]:
                best = (depth, sqrt_price, base_token, unit_is_token0, pool_id, base_dec,
                        evidence_ts)
        if best is None:
            silent = [p for p in pools if p[0] not in taped][:MAX_HOP_POOLS]
            for pool_id, base_token, unit_is_token0 in silent:
                if summary["reads"] + 2 > max_reads:
                    break
                sqrt_price, liquidity = read_pool_state(chain, pool_id, rpc_url, http=http, rpc_call=rpc_call)
                summary["reads"] += 2
                depth, base_dec = _depth(base_token, unit_is_token0, sqrt_price, liquidity)
                if depth < min_depth_usd:
                    continue
                if best is None or depth > best[0]:
                    # A STORAGE READ IS EVIDENCE FROM NOW. Its own leg is as fresh as this pass;
                    # the base leg below can still be older, and the oldest of the two is what the
                    # price actually rests on.
                    best = (depth, sqrt_price, base_token, unit_is_token0, pool_id, base_dec,
                            float(now))
        if best is None:
            summary["skipped"][unit] = "no_hop_above_floor"
            continue
        _depth_usd, sqrt_price, base_token, unit_is_token0, pool_id, base_dec, evidence_ts = best
        in_base = unit_price_in_base(sqrt_price, unit_is_token0=unit_is_token0,
                                     unit_decimals=decimals, base_decimals=base_dec)
        if in_base is None:
            summary["skipped"][unit] = "degenerate_price"
            continue
        usd = in_base * bases[base_token]
        if not math.isfinite(usd) or usd <= 0:
            summary["skipped"][unit] = "degenerate_price"
            continue
        conn.execute("INSERT OR REPLACE INTO volume_quote_prices (chain_id, token, usd, observed)"
                     " VALUES (?,?,?,?)",
                     (chain, unit, float(usd), now))
        # THE PRICE'S REAL AGE, BESIDE THE ROW RATHER THAN IN IT (F4, 2026-09-23). `observed` still
        # carries the write time, deliberately and for now: the gate compares `at - observed`
        # against MEMECOIN_ONCHAIN_M5_HOP_PRICE_MAX_AGE_S (180), so writing the true age into that
        # column IS the coverage change, and that is the operator's ruling to make, not this
        # commit's. Measured share of hop pools whose newest state is inside each limit, 4663,
        # 3,267 pools: 180 s 20.5%, 300 s 27.1%, 600 s 38.9%, 900 s 48.5%, 1800 s 71.9%.
        #
        # TODO(operator ruling, 2026-09-23): once he rules on the limit, the gate reads
        # `volume_quote_price_evidence.evidence_at` instead of `observed` and this comment goes.
        # Until then the honest age accrues here so the ruling is made on data rather than on a
        # guess, and nothing downstream reads it yet.
        #
        # THE OLDEST LEG DECIDES. A hop price is the pool state times the base unit's own price, so
        # it is exactly as old as the older of the two. A missing base time falls back to the pool's
        # own evidence rather than to `now`, because an unknown age must never render as fresh.
        _oldest = float(evidence_ts)
        _base_at = base_times.get(base_token)
        if _base_at is not None:
            _oldest = min(_oldest, float(_base_at))
        conn.execute("INSERT OR REPLACE INTO volume_quote_price_evidence "
                     "(chain_id, token, evidence_at, source) VALUES (?,?,?,?)",
                     (chain, unit, _oldest, "tape" if evidence_ts < float(now) else "chain"))
        summary["priced"][unit] = float(usd)
        priced[unit] = float(usd)
        summary["resolved"] += resolve_quote_sides(conn, chain, unit, decimals, now, bases=priced)
        # COMMIT PER UNIT, NEVER ACROSS A CHAIN READ (2026-09-22, release 2dca240c). One commit at
        # the end held the store's write lock for the whole pass -- 32 units and up to 160 reads,
        # 20-50 s -- while the socket loop on its own connection needed it for every header, and
        # the loop died with "database is locked" once a minute until the watchdog relaunched it.
        conn.commit()
    conn.commit()
    return summary

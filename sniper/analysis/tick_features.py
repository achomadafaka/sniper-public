"""Tick-tape feature computation v0 — offline, research-grade, no live consumer.

Spec: docs/superpowers/specs/2026-07-23-memecoin-tick-stream-design.md (phase 1). Computes
per-(chain, pool, window) microstructure features from the raw pool_ticks tape:

  swaps_n / makers_n / arrival_accel   — activity + its second derivative (die-out visibility)
  buy_n / sell_n / net_flow_quote      — order-flow imbalance in QUOTE-token units (orientation
                                          from pool_meta; buys of the coin = quote INTO the pool)
  wash_n                                — swaps excluded by the wash filter (same maker on both
                                          sides of the same pool inside the window, or dust)
  reserve_quote                         — latest V2 Sync quote reserve (LP-pull visibility)

The tape is adversarial (spec wall #3): every feature row records how much flow the wash filter
removed, so a model can never unknowingly train on the raw counts. Features have NO consumer in
any entry/exit path; the distribution-state detector gets its own pre-registered read first.

Pool orientation (token0/token1) is immutable per pool — resolved once via eth_call and cached
in pool_meta. Pools whose quote side is not a known quote token are skipped (features would be
unit-less noise).
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger("memecoin")

SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_DECIMALS = "0x313ce567"   # decimals(). 0x-prefixed to match SEL_TOKEN0/1 in this module —
                              # evm_wallet.SEL_DECIMALS is bare and would fail silently here.

# Factory pool-creation events — token addresses are INDEXED topics, so one eth_getLogs filtered by
# (topic0, token-in-topic1|topic2) finds every pool for a mint directly. This closes the coverage
# hole where the busy-pool resolver (top-200 by recent activity) never reaches small/short-lived
# coins: exactly the slow-entry cohort (missingness audit 2026-07-26 — 4x worse median PnL).
V2_PAIRCREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
V3_POOLCREATED = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"


def ensure_feature_tables(tc):
    tc.execute("""
        CREATE TABLE IF NOT EXISTS pool_meta (
            chain_id TEXT NOT NULL,
            pool TEXT NOT NULL,
            token0 TEXT,
            token1 TEXT,
            quote_side INTEGER,            -- 0|1 = which token is the quote asset; NULL = unknown
            resolved_at TEXT NOT NULL,
            PRIMARY KEY (chain_id, pool)
        )""")
    # The live ticks DB already has pool_meta, so CREATE TABLE IF NOT EXISTS alone never adds these.
    # NULL = decimals unresolved -> no tape price is produced (fail-closed, never a guessed scale).
    for _col in ("token0_decimals", "token1_decimals"):
        try:
            tc.execute(f"ALTER TABLE pool_meta ADD COLUMN {_col} INTEGER")
        except sqlite3.OperationalError:
            pass          # already present
    # WHY A POOL HAS NO PAIR, which decides whether it is ever asked again (2026-09-23 review, F5).
    # 'reverted' = the node answered that the contract refuses, a property of the contract, so the
    # row is permanent. 'ambiguous' = the call returned `0x` or a short string, which a contract
    # without token0() and a node that cannot serve the state both produce, so the row expires and
    # is re-asked. NULL = a resolved pool, or a row written before this column existed.
    try:
        tc.execute("ALTER TABLE pool_meta ADD COLUMN resolve_state TEXT")
    except sqlite3.OperationalError:
        pass
    # THE REST OF THE PoolKey (2026-09-24). `register_pool` decodes the full key from the V4
    # `Initialize` event and VERIFIES it -- `v4_pool_id(key) != pool` raises there -- and then kept
    # only the currencies. Without fee, tickSpacing and hooks nothing downstream can rebuild the
    # key, so the entry ladder had to relearn it from a provider or an `eth_getLogs`: measured on
    # 4663, the keys learned inside one retained hour arrived a median 346 s AFTER the pool's first
    # tick (p90 417 s), which is exactly the window a freshly graduated coin trades in. NULL on
    # every row written before this column existed, and `memecoin_v4_key_seed` skips those rather
    # than guessing a fee that would hash to a different pool.
    for _col, _ddl in (("fee", "INTEGER"), ("tick_spacing", "INTEGER"), ("hooks", "TEXT")):
        try:
            tc.execute(f"ALTER TABLE pool_meta ADD COLUMN {_col} {_ddl}")
        except sqlite3.OperationalError:
            pass
    tc.execute("""
        CREATE TABLE IF NOT EXISTS pool_features (
            id INTEGER PRIMARY KEY,
            chain_id TEXT NOT NULL,
            pool TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_s REAL NOT NULL,
            swaps_n INTEGER NOT NULL,
            wash_n INTEGER NOT NULL,
            makers_n INTEGER NOT NULL,
            buy_n INTEGER NOT NULL,
            sell_n INTEGER NOT NULL,
            net_flow_quote TEXT,
            arrival_accel REAL,
            reserve_quote TEXT,
            computed_at TEXT NOT NULL,
            UNIQUE(chain_id, pool, window_start, window_s)
        )""")
    tc.commit()


def resolve_pool_meta(tc, chain_id, pools, eth_call, quote_tokens, now_iso, *, budget=50):
    """Fill pool_meta for unknown pools (bounded per run). `eth_call(pool, selector)` returns the
    raw hex result or None. A failed call leaves the pool unresolved for a later run — never a
    guessed orientation."""
    # "Done" means: checked AND nothing left to learn. A pool with quote_side NULL has been checked
    # and is not usable (its quote is not a known quote asset) — it stays done so it never consumes
    # the budget again. A USABLE pool still missing decimals is NOT done: pools resolved before the
    # decimals columns existed would otherwise keep their NULLs forever and never price from tape.
    # ponytail: a token whose decimals() permanently fails is retried each run; bounded by `budget`
    # and by how few such tokens exist. Add a failure marker column if that ever starves new pools.
    quotes_l = sorted({str(q).lower() for q in quote_tokens})
    # Retro-upgrade: pools checked before a quote token entered the known set sit at quote_side
    # NULL forever ("done" semantics). Their token0/token1 are already stored — fix orientation
    # with pure SQL whenever the quote set widens (USDG on 4663, coverage audit 2026-07-26).
    for side, col in ((0, "token0"), (1, "token1")):
        tc.execute(
            f"UPDATE pool_meta SET quote_side={side} WHERE chain_id=? AND quote_side IS NULL "
            f"AND LOWER({col}) IN ({','.join('?' * len(quotes_l))})",
            (str(chain_id), *quotes_l))
    tc.commit()
    known = {r["pool"] for r in tc.execute(
        "SELECT pool FROM pool_meta WHERE chain_id=? AND (quote_side IS NULL OR "
        "(token0_decimals IS NOT NULL AND token1_decimals IS NOT NULL))", (str(chain_id),))}
    todo = [p for p in pools if p not in known][:budget]
    quotes = {str(q).lower() for q in quote_tokens}
    resolved = 0
    pending = []
    for pool in todo:
        t0 = eth_call(pool, SEL_TOKEN0)
        t1 = eth_call(pool, SEL_TOKEN1)
        if not t0 or not t1:
            continue
        token0 = ("0x" + t0[-40:]).lower()
        token1 = ("0x" + t1[-40:]).lower()
        quote_side = 0 if token0 in quotes else 1 if token1 in quotes else None

        def _dec(token):
            raw = eth_call(token, SEL_DECIMALS)
            if not raw:
                return None                     # failed call -> unresolved, never guessed
            try:
                v = int(raw, 16)
            except (TypeError, ValueError):
                return None
            # ERC-20 decimals is a uint8. A value outside that range means the call did NOT return
            # a decimals word (contract lacks decimals(), or the node echoed something else) — a
            # garbage scale is worse than none, so fail closed. Also keeps SQLite from overflowing
            # on an address-shaped response.
            return v if 0 <= v <= 255 else None

        # Collect ONLY — do not write yet. Writing per pool held the ticks-DB write lock open across
        # every eth_call below (network, seconds each, up to `budget` of them), which starved the
        # tickstream's set_cursor for minutes: "database is locked" went 1/day -> 3,333 on
        # 2026-07-24, the day this job first ran. SQLite WAL allows one writer; a writer that waits
        # on the network is a writer that blocks the tape. All I/O first, then one short write.
        pending.append((str(chain_id), pool, token0, token1, quote_side, now_iso,
                        _dec(token0), _dec(token1)))
        resolved += 1
    if pending:
        tc.executemany(
            "INSERT OR REPLACE INTO pool_meta (chain_id, pool, token0, token1, quote_side,"
            " resolved_at, token0_decimals, token1_decimals) VALUES (?,?,?,?,?,?,?,?)", pending)
        tc.commit()
    return resolved


def pools_for_token_via_factory(rpc_call, token, *, lookback_blocks=8_000_000,
                                chunk_blocks=1_500_000, pace_s=0.2):
    """V2 pairs / V3 pools created for `token`, from factory-creation logs, scanned in bounded
    chunks over the last `lookback_blocks`. Full-range topic queries TIME OUT on the 4663 RPC and
    return {'error': ...} with result None — which must never be read as 'no pools' (that exact
    misread hid 12 busy coins, 2026-07-26). A failed chunk is skipped; [] means nothing FOUND, not
    nothing exists. 8M blocks ≈ 9 days at ~10 blocks/s — covers any coin we entered recently."""
    topic = "0x" + str(token).lower().replace("0x", "").rjust(64, "0")
    try:
        tip = int(rpc_call("eth_blockNumber", []).get("result"), 16)
    except Exception:
        return []
    start = max(0, tip - lookback_blocks)
    pools = []
    for ev, data_word in ((V2_PAIRCREATED, 0), (V3_POOLCREATED, 1)):
        for topics in ([ev, topic], [ev, None, topic]):
            for lo in range(start, tip + 1, chunk_blocks):
                hi = min(lo + chunk_blocks - 1, tip)
                if pace_s:
                    # the shared ingest RPC silently returns EMPTY under burst load (not an
                    # error) — pacing is correctness here, not politeness (2026-07-26)
                    import time
                    time.sleep(pace_s)
                try:
                    resp = rpc_call("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                                     "topics": topics}])
                except Exception:
                    continue
                logs = resp.get("result") if isinstance(resp, dict) else None
                for log in logs if isinstance(logs, list) else []:
                    data = str(log.get("data") or "")[2:]
                    word = data[data_word * 64:(data_word + 1) * 64]
                    if len(word) == 64:
                        pools.append(("0x" + word[-40:]).lower())
    return sorted(set(pools))


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def pools_for_token_via_transfers(rpc_call, token, *, lookback_blocks=2_000_000,
                                  chunk_blocks=500_000, top_n=3, pace_s=0.2):
    """Candidate pools from the mint's own Transfer counterparties. Launchpad pools on 4663 are
    deployed WITHOUT a factory event yet emit standard V2/V3 swaps (verified 2026-07-26, pool
    0x6426…: 1797 V3_SWAP logs); the pool is the mint's top transfer counterparty. Non-pool
    candidates (routers, wallets) fail the token0()/token1() eth_call downstream and drop out.
    Scanned in bounded chunks (one oversized request just times out and retrying it verbatim
    re-times-out — Codex review 2026-07-26). Returns `(candidates, complete)`; a failed or malformed
    chunk makes the whole ranking incomplete so partial counts can never select a false top pool."""
    import time
    from collections import Counter
    try:
        tip = int(rpc_call("eth_blockNumber", []).get("result"), 16)
    except Exception:
        return [], False
    start = max(0, tip - lookback_blocks)
    counts = Counter()
    complete = True
    zero = "0x" + "0" * 40
    for lo in range(start, tip + 1, chunk_blocks):
        hi = min(lo + chunk_blocks - 1, tip)
        if pace_s:
            time.sleep(pace_s)
        try:
            resp = rpc_call("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                             "address": str(token).lower(),
                                             "topics": [TRANSFER_TOPIC]}])
        except Exception:
            complete = False
            continue
        logs = resp.get("result") if isinstance(resp, dict) else None
        if not isinstance(logs, list):
            complete = False
            continue
        for log in logs:
            topics = log.get("topics") or []
            for topic in topics[1:3]:
                addr = "0x" + str(topic)[-40:]
                if addr != zero:
                    counts[addr] += 1
    return [a for a, _n in counts.most_common(top_n)], complete


def resolve_mint_pools(tc, chain_id, mints, rpc_call, eth_call, quote_tokens, now_iso, *,
                       budget=10):
    """Targeted pool_meta resolution for specific mints (entered coins the busy-pool resolver
    misses). For each unmapped mint: factory-log lookup -> resolve those pools. Bounded by
    `budget` mints per run; already-mapped mints cost one SQL check. -> mints resolved."""
    attempts = resolved = 0
    for mint in mints:
        if attempts >= budget:
            break
        mint = str(mint).lower()
        # Skip only on a USABLE mapping (oriented quote pool). A mint whose only rows are
        # coin-vs-coin pools (quote_side NULL) must keep retrying — its real quote pool may sit in
        # a factory-log chunk that timed out on a previous run.
        if tc.execute(
                "SELECT 1 FROM pool_meta WHERE chain_id=? AND quote_side IS NOT NULL AND "
                "((quote_side=0 AND LOWER(token1)=?) OR (quote_side=1 AND LOWER(token0)=?)) "
                "LIMIT 1", (str(chain_id), mint, mint)).fetchone():
            continue
        # An ATTEMPT consumes budget whether or not candidates are found — otherwise permanently
        # unresolvable mints make every cron run scan the whole backlog (unbounded RPC; Codex
        # review 2026-07-26).
        attempts += 1
        candidates = pools_for_token_via_factory(rpc_call, mint)
        if not candidates:
            candidates, complete = pools_for_token_via_transfers(rpc_call, mint)
            if not complete:
                candidates = []
        if not candidates:
            continue
        resolve_pool_meta(tc, chain_id, candidates, eth_call, quote_tokens, now_iso,
                          budget=len(candidates))
        resolved += 1
    return resolved


def compute_window_features(swaps, *, quote_side, dust_quote_atomic=0):
    """Pure: one pool-window's swap rows -> feature dict. Each row needs maker, amount0, amount1
    (signed TEXT, positive = INTO the pool). A buy of the coin pays quote INTO the pool
    (quote amount > 0); a sell receives quote OUT (< 0).

    Wash filter v0: (a) makers appearing on BOTH sides of this pool inside the window are
    excluded entirely (round-trippers); (b) swaps with |quote| <= dust threshold are excluded.
    wash_n reports how much was removed — honesty stat, always carried with the features."""
    qi = f"amount{quote_side}"
    parsed = []
    for s in swaps:
        try:
            q = int(s[qi]) if s[qi] is not None else 0
        except (TypeError, ValueError):
            continue
        parsed.append((str(s["maker"] or ""), q))
    both_sides = {m for m, q in parsed if q > 0} & {m for m, q in parsed if q < 0}
    both_sides.discard("")
    clean, wash = [], 0
    for maker, q in parsed:
        if maker in both_sides or abs(q) <= dust_quote_atomic:
            wash += 1
        else:
            clean.append((maker, q))
    buys = [q for _m, q in clean if q > 0]
    sells = [q for _m, q in clean if q < 0]
    return {
        "swaps_n": len(parsed),
        "wash_n": wash,
        "makers_n": len({m for m, _q in clean if m}),
        "buy_n": len(buys),
        "sell_n": len(sells),
        "net_flow_quote": str(sum(q for _m, q in clean)),
    }


def compute_pool_windows(tc, chain_id, pool, *, window_s, now_iso, quote_side,
                         dust_quote_atomic=0, windows=2):
    """Compute the most recent `windows` fixed windows ending at now for one pool; returns rows
    (newest last). arrival_accel = swaps_n(newest) − swaps_n(previous) — the second derivative
    the die-out read needs."""
    from datetime import datetime, timedelta
    end = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    out = []
    counts = []
    for w in range(windows, 0, -1):
        w_end = end - timedelta(seconds=window_s * (w - 1))
        w_start = w_end - timedelta(seconds=window_s)
        swaps = tc.execute(
            "SELECT maker, amount0, amount1 FROM pool_ticks WHERE chain_id=? AND pool=? "
            "AND kind IN ('v2_swap','v3_swap') AND observed_at > ? AND observed_at <= ?",
            (str(chain_id), pool, w_start.isoformat(), w_end.isoformat())).fetchall()
        feats = compute_window_features(
            swaps, quote_side=quote_side, dust_quote_atomic=dust_quote_atomic)
        counts.append(feats["swaps_n"])
        reserve = tc.execute(
            "SELECT amount0, amount1 FROM pool_ticks WHERE chain_id=? AND pool=? "
            "AND kind='v2_sync' AND observed_at <= ? ORDER BY observed_at DESC LIMIT 1",
            (str(chain_id), pool, w_end.isoformat())).fetchone()
        out.append({
            "chain_id": str(chain_id), "pool": pool,
            "window_start": w_start.isoformat(), "window_s": float(window_s),
            **feats,
            "arrival_accel": None,
            "reserve_quote": (reserve[f"amount{quote_side}"] if reserve else None),
            "computed_at": now_iso,
        })
    if len(out) >= 2:
        out[-1]["arrival_accel"] = float(counts[-1] - counts[-2])
    return out


def store_features(tc, rows):
    if not rows:
        return 0
    before = tc.total_changes
    tc.executemany(
        "INSERT OR REPLACE INTO pool_features (chain_id, pool, window_start, window_s, swaps_n,"
        " wash_n, makers_n, buy_n, sell_n, net_flow_quote, arrival_accel, reserve_quote,"
        " computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["chain_id"], r["pool"], r["window_start"], r["window_s"], r["swaps_n"], r["wash_n"],
          r["makers_n"], r["buy_n"], r["sell_n"], r["net_flow_quote"], r["arrival_accel"],
          r["reserve_quote"], r["computed_at"]) for r in rows])
    tc.commit()
    return tc.total_changes - before


def tick_liquidity_usd(tc, chain_id, pool, *, quote_side, quote_decimals, quote_usd):
    """Latest V2-reserve liquidity in USD for one pool (quote reserve x2, the DexScreener
    convention), or None. Feeds memecoin_token_liquidity through the EXISTING paper-slippage
    enrichment path — closing the 4663 coverage gap without touching the money path."""
    row = tc.execute(
        "SELECT amount0, amount1 FROM pool_ticks WHERE chain_id=? AND pool=? AND kind='v2_sync' "
        "ORDER BY observed_at DESC LIMIT 1", (str(chain_id), pool)).fetchone()
    if not row:
        return None
    try:
        reserve = int(row[f"amount{quote_side}"])
    except (TypeError, ValueError):
        return None
    if reserve <= 0 or not quote_usd or quote_usd <= 0:
        return None
    return (reserve / (10 ** int(quote_decimals))) * float(quote_usd) * 2.0

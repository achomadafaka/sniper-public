"""Base 8453 fast lane, STEP 0: the sign-less decide in the 8453 watcher, recorded only.

Spec docs/superpowers/specs/2026-10-02-memecoin-base-8453-fast-lane-design.md (rev 3, Gamma PASS
3d253ebd8; Alpha order: build step 0, record only). Nothing here sends, signs, reserves or reads a
network host. The 8453 watcher calls `decide` on its push socket's loop thread AFTER today's
detection hand-off (commit and wake), with the receipt and transaction it already holds, and queues
the record to `DecisionWriter`, whose own thread writes `base_fast_decisions` in this lane's own
SQLite file (never the ledger).

THE DECIDE, from the source buy's own receipt:
- the coin's leg is the PoolManager `Swap` of a V4 pool B that has the coin as a currency. Its
  sqrtPriceX96 and liquidity are B's exact post-trade state, at zero lag;
- B's PoolKey comes first from the source transaction's own calldata (every Base source buy goes
  through the Relay router, and the key sits in its calldata as consecutive words), accepted only
  when keccak(abi.encode(key)) is B's pool id; else from the stored key (the watcher's key reader);
  else `key_unknown`;
- the route kind is B's other currency: native `v4_native`, WETH `v4_weth`, USDC `v4_bridge`
  (hop A native -> USDC priced at the watcher's ETH/USD mark, USDC = $1), anything else
  `bridge_unsupported`;
- cap and min_out are the 4663 rules (memecoin_rh_fast_decide.coin_price / cap_price / min_out_at):
  cap = min(post x (1 + chase), the source's own average on this hop x premium).

The ETH/USD mark is read cache-only (`NativePrice.peek()` in the watcher, passed in as value and
age): never a fetch. A mark older than MAX_MARK_AGE_S is `bridge_state_stale` on a bridge and
`eth_usd_stale` otherwise (the clip is sized in ETH from it).
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from fractions import Fraction

from eth_utils import keccak

logger = logging.getLogger(__name__)

CHAIN = "8453"
NATIVE = "0x" + "00" * 20
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
MAX_MARK_AGE_S = 30.0
_ADDR_MASK = (1 << 160) - 1


def mode():
    """MEMECOIN_BASE_FAST_LANE: off (default) | step0 | runner_shadow | live
    (settings.memecoin_base_fast_lane_mode). Every mode but off records; the two runner modes also
    send an enabled would-send decision to the runner's lane (memecoin_base_fast_lane)."""
    from sniper.settings import memecoin as cfg
    return cfg.memecoin_base_fast_lane_mode()


# --- the receipt ----------------------------------------------------------------------------------

def _signed128(word):
    v = int(word, 16) & ((1 << 128) - 1)
    return v - (1 << 128) if v >> 127 else v


def v4_swaps(receipt):
    """[{pool, amount0, amount1, sqrt, liquidity, tick, log_index}] of the PoolManager's Swap events,
    in log order. Amounts carry the SWAPPER's sign: positive is what the swapper received."""
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP
    manager = V4_POOL_MANAGERS[CHAIN]
    out = []
    for log in (receipt or {}).get("logs") or ():
        topics = log.get("topics") or ()
        if (len(topics) < 2 or str(topics[0]).lower() != V4_SWAP
                or str(log.get("address") or "").lower() != manager):
            continue
        data = str(log.get("data") or "0x")[2:]
        words = [data[i:i + 64] for i in range(0, len(data), 64)]
        if len(words) < 6:
            continue
        tick = int(words[4], 16) & ((1 << 24) - 1)
        out.append({"pool": str(topics[1]).lower(), "amount0": _signed128(words[0]),
                    "amount1": _signed128(words[1]), "sqrt": int(words[2], 16),
                    "liquidity": int(words[3], 16), "tick": tick - (1 << 24) if tick >> 23 else tick,
                    "log_index": int(str(log.get("logIndex") or "0x0"), 16)})
    return out


# --- PoolKeys from the source's own calldata -------------------------------------------------------

def _pool_id(c0, c1, fee, spacing, hooks):
    word = lambda v: (v % (1 << 256)).to_bytes(32, "big")       # noqa: E731
    return "0x" + keccak(word(c0) + word(c1) + word(fee) + word(spacing) + word(hooks)).hex()


def _addr(w):
    return w >> 160 == 0


def _int24(w):
    """The int24 a sign-extended ABI word holds, or None."""
    if w < (1 << 23):
        return w
    if w >= (1 << 256) - (1 << 23):
        return w - (1 << 256)
    return None


def calldata_keys(input_hex, wanted, mint):
    """{pool id: PoolKey dict} for the wanted pools that have `mint` as a currency and whose key sits
    in the calldata: exact or absent. A PoolKey is 5 consecutive words (currency0 < currency1, fee,
    tickSpacing, hooks); a PathKey is 4 (intermediate currency, fee, tickSpacing, hooks) whose other
    currency is the coin, or, when the intermediate IS the coin, one of the calldata's address words
    or native. Nested calls shift word alignment by their 4-byte selectors, so every 4-byte alignment
    is scanned. A candidate is accepted ONLY when its keccak is a wanted id. Only coin pairs are
    hashed, which keeps this CPU-cheap on the watcher's socket thread."""
    wanted = {str(w).lower() for w in wanted}
    try:
        raw = bytes.fromhex(str(input_hex or "")[2:])
        coin = int(str(mint), 16)
    except ValueError:
        return {}
    coin_bytes = coin.to_bytes(32, "big")
    found = {}
    for align in range(0, 32, 4):
        body = raw[align:]
        if coin_bytes not in body:
            continue                      # the coin never sits on a word boundary at this alignment
        words = [int.from_bytes(body[i:i + 32], "big") for i in range(0, len(body) - 31, 32)]
        addrs = None
        for i in range(len(words) - 3):
            w = words[i:i + 5]
            if (len(w) == 5 and coin in (w[0], w[1]) and _addr(w[0]) and _addr(w[1]) and w[0] < w[1]
                    and w[2] < (1 << 24) and _int24(w[3]) is not None and _addr(w[4])):
                pid = _pool_id(w[0], w[1], w[2], _int24(w[3]), w[4])
                if pid in wanted and pid not in found:
                    found[pid] = _key(w[0], w[1], w[2], _int24(w[3]), w[4])
                    if len(found) == len(wanted):
                        return found
                    continue
            mid, fee4, sp4, hooks = words[i], words[i + 1], _int24(words[i + 2]), words[i + 3]
            if not (_addr(mid) and fee4 < (1 << 24) and sp4 is not None and _addr(hooks)):
                continue
            if mid == coin:
                if addrs is None:
                    addrs = {x for x in words if _addr(x) and x != coin} | {0}
                others = addrs
            else:
                others = (coin,)
            for other in others:
                c0, c1 = sorted((mid, other))
                pid = _pool_id(c0, c1, fee4, sp4, hooks)
                if pid in wanted and pid not in found:
                    found[pid] = _key(c0, c1, fee4, sp4, hooks)
                    if len(found) == len(wanted):
                        return found
    return found


def _key(c0, c1, fee, spacing, hooks):
    hexa = lambda v: "0x%040x" % (v & _ADDR_MASK)                # noqa: E731
    return {"currency0": hexa(c0), "currency1": hexa(c1), "fee": int(fee), "tick_spacing": int(spacing),
            "hooks": hexa(hooks)}


# --- the decide ---------------------------------------------------------------------------------

def _f(x):
    return None if x is None else [x.numerator, x.denominator]


def decide(tx, receipt, *, mint, eth_usd, eth_usd_age_s, clip_usd, chase_pct, premium, key_fn=None):
    """One step-0 decision. Never raises, never reads a network host. Returns the record:
    reason ('would_send' or a named fallback), route_kind, key_source, pool, block, B's post-state,
    the source's own in/out on B, cap [num, den], min_out, clip in X atomic, a_source and the mark's
    age, decide_ms."""
    from sniper.services.memecoin_rh_fast_decide import cap_price, coin_price, min_out_at
    t0 = time.perf_counter()
    mint = str(mint or "").lower()
    rec = {"reason": None, "mint": mint, "eth_usd": eth_usd, "eth_usd_age_s": eth_usd_age_s,
           "clip_usd": clip_usd}

    def done(reason, **fields):
        rec.update(fields, reason=reason, decide_ms=(time.perf_counter() - t0) * 1000.0)
        return rec
    try:
        if not isinstance(receipt, dict) or str(receipt.get("status") or "").lower() not in ("0x1", "1"):
            return done("reverted_or_no_receipt")
        rec["block"] = int(str(receipt.get("blockNumber") or "0x0"), 16) or None
        swaps = v4_swaps(receipt)
        if not swaps:
            return done("no_v4_leg")
        ids = {s["pool"] for s in swaps}
        keys = calldata_keys((tx or {}).get("input"), ids, mint)
        source = {pid: "calldata" for pid in keys}
        for pid in ids - set(keys):
            key = key_fn(CHAIN, pid) if key_fn is not None else None
            if key:
                keys[pid], source[pid] = key, "store"
        coin = [s for s in swaps if s["pool"] in keys
                and mint in (str(keys[s["pool"]]["currency0"]).lower(), str(keys[s["pool"]]["currency1"]).lower())]
        rec["v4_pools"], rec["keys_known"] = len(ids), len(keys)
        if not coin:
            return done("key_unknown" if len(keys) < len(ids) else "no_v4_coin_leg")
        if len({s["pool"] for s in coin}) > 1:
            return done("leg_ambiguous")
        # B's post-state is its LAST swap in the transaction: a hook may trade the pool after the
        # source (a dynamic-fee hook selling back, seen on Base). The source's average is the sum of
        # the swaps on B that paid the coin out, never the whole transaction.
        b = coin[-1]
        key = keys[b["pool"]]
        coin_is_c0 = str(key["currency0"]).lower() == mint
        x = str(key["currency1"] if coin_is_c0 else key["currency0"]).lower()
        legs = [((s["amount0"], s["amount1"]) if coin_is_c0 else (s["amount1"], s["amount0"])) for s in coin]
        buys = [(c, xa) for c, xa in legs if c > 0 and xa < 0]
        rec.update(pool=b["pool"], key_source=source[b["pool"]], key=key, post_sqrt=str(b["sqrt"]),
                   liquidity=str(b["liquidity"]), tick=b["tick"], x=x, legs_on_pool=len(coin),
                   buy_legs=len(buys), swap_log_index=b["log_index"])
        kind = {NATIVE: "v4_native", WETH: "v4_weth", USDC: "v4_bridge"}.get(x)
        rec["route_kind"] = kind
        if kind is None:
            return done("bridge_unsupported")
        if b["liquidity"] <= 0:
            return done("no_liquidity")
        if not buys:
            return done("not_a_buy")
        src_in, src_out = -sum(xa for _c, xa in buys), sum(c for c, _xa in buys)
        rec.update(src_in=str(src_in), src_out=str(src_out))
        stale = (eth_usd is None or eth_usd <= 0 or eth_usd_age_s is None
                 or eth_usd_age_s > MAX_MARK_AGE_S)
        rec["a_source"] = "eth_usd_mark"
        if stale:
            return done("bridge_state_stale" if kind == "v4_bridge" else "eth_usd_stale")
        # The coin is bought with X; the pool pays out the coin, so X is the side that came in.
        post = coin_price(b["sqrt"], in_is_c0=not coin_is_c0)
        cap = cap_price(post, src_in, src_out, chase_pct=chase_pct, premium=premium)
        if cap is None:
            return done("no_price_cap")
        if not clip_usd or clip_usd <= 0:
            return done("clip_zero")
        clip_wei = int(Fraction(str(clip_usd)) / Fraction(str(eth_usd)) * 10 ** 18)
        # Hop A native -> USDC at the mark, USDC at $1 (6 dp): x_mid is what the bridge would hand
        # B. On native and WETH routes the clip itself is X.
        clip_x = clip_wei if kind != "v4_bridge" else int(Fraction(clip_wei) * Fraction(str(eth_usd)) / 10 ** 12)
        min_out = min_out_at(clip_x, cap)
        rec.update(cap=_f(cap), post_price=_f(post), clip_wei=str(clip_wei), clip_x=str(clip_x),
                   min_out=str(min_out))
        if not min_out:
            return done("min_out_zero")
        return done("would_send")
    except Exception as exc:                   # a decide fault is a record, never a raise
        logger.exception("base fast lane: decide failed mint=%s", mint)
        return done("error:%s" % type(exc).__name__)


# --- the record, off the socket thread ----------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS base_fast_decisions(
  id INTEGER PRIMARY KEY, decided_at TEXT NOT NULL, tx_hash TEXT NOT NULL, mint TEXT, wallet TEXT,
  source_user_id TEXT, handle TEXT, block INTEGER, reason TEXT NOT NULL, route_kind TEXT,
  key_source TEXT, pool TEXT, post_sqrt TEXT, liquidity TEXT, tick INTEGER, src_in TEXT, src_out TEXT,
  cap_num TEXT, cap_den TEXT, min_out TEXT, clip_x TEXT, a_source TEXT, eth_usd REAL,
  eth_usd_age_s REAL, detection_price_source TEXT, decide_ms REAL, record_json TEXT,
  path TEXT, verify_ms REAL, arrival_at TEXT, since_arrival_ms REAL,
  UNIQUE(tx_hash, mint));
CREATE INDEX IF NOT EXISTS base_fast_decisions_at ON base_fast_decisions(decided_at);
"""


class DecisionWriter:
    """One producer (the socket thread: `put`, never blocks) and one consumer (this thread, which
    owns the only connection to `path`). A full queue drops the record and counts it."""

    def __init__(self, path, *, maxsize=1000, connect=None):
        self.path, self.dropped, self.written = str(path), 0, 0
        self._q = queue.Queue(maxsize=maxsize)
        self._connect = connect or (lambda p: sqlite3.connect(p, timeout=30))
        self._thread = threading.Thread(target=self._run, name="base-fast-writer", daemon=True)
        self._thread.start()

    def put(self, rec):
        try:
            self._q.put_nowait(rec)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _run(self):
        db = self._connect(self.path)
        db.executescript(DDL)
        while True:
            rec = self._q.get()
            if rec is None:
                break
            try:
                write(db, rec)
                self.written += 1
            except Exception:
                logger.exception("base fast lane: record write failed tx=%s", rec.get("tx_hash"))
            finally:
                self._q.task_done()
        db.close()

    def flush(self, timeout_s=5.0):
        deadline = time.monotonic() + timeout_s
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self):
        self._q.put(None)
        self._thread.join(timeout=5.0)


COLS = ("decided_at", "tx_hash", "mint", "wallet", "source_user_id", "handle", "block", "reason",
        "route_kind", "key_source", "pool", "post_sqrt", "liquidity", "tick", "src_in", "src_out",
        "cap_num", "cap_den", "min_out", "clip_x", "a_source", "eth_usd", "eth_usd_age_s",
        "detection_price_source", "decide_ms", "record_json", "path", "verify_ms", "arrival_at",
        "since_arrival_ms")


def write(db, rec):
    row = dict(rec)
    cap = row.get("cap") or [None, None]
    row["cap_num"], row["cap_den"] = (None if cap[0] is None else str(cap[0]),
                                      None if cap[1] is None else str(cap[1]))
    row["record_json"] = json.dumps(rec, sort_keys=True, default=str)
    db.execute("INSERT OR IGNORE INTO base_fast_decisions (%s) VALUES (%s)"
               % (",".join(COLS), ",".join("?" * len(COLS))), [row.get(c) for c in COLS])
    db.commit()


def lane_clip_usd():
    """The step-0 clip: the lane's pilot clip, bound by the funded 8453 per-entry ceiling when set."""
    from sniper.settings import memecoin as cfg
    pilot = float(cfg.MEMECOIN_BASE_FAST_LANE_CLIP_USD)
    ceiling = float(cfg.evm_max_clip_usd_for(CHAIN) or 0)
    return min(pilot, ceiling) if ceiling > 0 else pilot


def lane_datagram(rec, row, receipt, *, eth_usd, eth_usd_at, sent_epoch, routes, chase_pct, premium):
    """(the runner lane's datagram, None) for one would-send record of an enabled kind, else
    (None, why). Pure and in memory (step 1, plan 2026-10-14 part C). The datagram has the 4663
    native shape the shared lane checks: the receipt-backed wallet fill (source_fill, bound to the
    sealed block), the source's post state on B as the record found it, and a cap no looser than
    either the spec's hop rule or the lane's own wallet-net check."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.services.memecoin_rh_fast_decide import cap_price, min_out_at
    from sniper.services.memecoin_source_fill import source_fill_from_buy, validate_source_fill
    if rec.get("reason") != "would_send":
        return None, "not_would_send"
    kind = rec.get("route_kind")
    if kind not in routes:
        return None, "route_off:%s" % kind
    if (row or {}).get("observation_state") == "retracted":
        return None, "source_retracted"
    block_hash = str((receipt or {}).get("blockHash") or "").lower()
    try:
        preconf = len(block_hash) == 66 and int(block_hash, 16) == 0
    except ValueError:
        return None, "no_block"
    if len(block_hash) != 66:
        return None, "no_block"
    # A Flashblocks-preconfirmed source carries the zero hash and acts like the ordinary path, which
    # buys at preconfirmation (Alpha ruling 2026-10-14); the runner re-checks for a retraction.
    buy = (row or {}).get("buy") or {}
    sig = dict(chain=CHAIN, tx_hash=str(row.get("tx_hash") or "").lower(),
               wallet=str(row.get("wallet") or "").lower(), source_user_id=row.get("source_user_id"),
               mint=rec["mint"], seq=rec.get("block"), block_hash=block_hash)
    fill = validate_source_fill(source_fill_from_buy(buy, sig), sig)
    if fill is None:
        return None, "source_fill_unproved"
    post = fill.get("source_post") or {}
    if (post.get("venue") != "v4" or post.get("pool") != rec.get("pool")
            or str(post.get("sqrt_price_x96")) != str(rec.get("post_sqrt"))):
        return None, "source_post_unbound"
    eth = Fraction(str(eth_usd))
    if fill["payment_token"] == CHAIN_QUOTE[CHAIN]["token"].lower() and fill["payment_decimals"] == 18:
        src_in = int(fill["payment_atomic"])
    else:                                   # the lane's own rule (native_source_price_matches)
        src_in = int(Fraction(str(fill["paid_usd"])) / eth * 10 ** 18)
    acquired = int(fill["acquired_atomic"])
    post_price = Fraction(*rec["post_price"])
    caps = [c for c in (Fraction(*rec["cap"]),
                        cap_price(post_price, src_in, acquired, chase_pct=chase_pct, premium=premium))
            if c is not None]
    cap = min(caps) if src_in > 0 else None
    clip_wei = int(rec.get("clip_wei") or 0)
    min_out = min_out_at(clip_wei, cap) if cap else None
    if not cap or not min_out or clip_wei <= 0:
        return None, "no_price_cap"
    key = rec["key"]
    payload = {"chain": CHAIN, "tx_hash": sig["tx_hash"], "seq": int(rec["block"]),
               "wallet": sig["wallet"], "mint": rec["mint"], "source_user_id": row.get("source_user_id"),
               "handle": row.get("handle"), "arrival_utc": row.get("now"), "route_kind": kind,
               "key": key, "pool": rec["pool"],
               "in_is_c0": str(key["currency0"]).lower() == rec.get("x"),
               "cap_num": cap.numerator, "cap_den": cap.denominator, "clip_wei": clip_wei,
               "min_out": int(min_out), "post_sqrt": int(rec["post_sqrt"]), "src_in_wei": src_in,
               "state_liquidity": int(rec["liquidity"]) if rec.get("liquidity") is not None else None,
               "src_out": str(acquired), "post_wei_num": post_price.numerator,
               "post_wei_den": post_price.denominator, "paid_usd": fill["paid_usd"],
               "source_fill": fill, "eth_usd": float(eth_usd), "eth_usd_at": float(eth_usd_at),
               "sent_epoch": float(sent_epoch), "preconf": preconf}
    from sniper.services.memecoin_bsc_fast_lane import _receipt_metadata
    payload.update(_receipt_metadata(row))
    return payload, None


def _since_ms(arrival_at, decided_at):
    try:
        return (datetime.fromisoformat(decided_at) - datetime.fromisoformat(arrival_at)).total_seconds() * 1000.0
    except (TypeError, ValueError):
        return None


def _retracted(payload):
    """'source_retracted' when the watcher's ledger row for this source is a proven phantom, else None
    (memecoin_base_preconf.presign_check, the executor's own rule)."""
    from sniper.services.memecoin_base_preconf import presign_check
    check = presign_check("rhfast:%s:%s:%s" % (CHAIN, payload["tx_hash"], payload["mint"]))
    return check() if check is not None else None


def make_hook(writer, *, key_fn=None, clip_usd_fn=None, clock=None, send_fn=None, host_clock=time.time,
              retracted_fn=_retracted):
    """The watcher's step-0 hook: `hook(row, tx, receipt, eth_usd, eth_usd_age_s, price_source,
    path=, verify_ms=)` decides, queues the record and returns it. It never raises and never touches
    the network. It runs on the push socket's thread (path 'direct') and on the Relay origin workers
    after verification (path 'relay_verified', Alpha 2026-10-07): `since_arrival_ms`, from the
    source's arrival to this decide, counts the verify wait a live lane could not skip.

    In runner_shadow or live (step 1) an enabled would-send decision is also sent as one datagram
    to the runner's lane (`send_fn(payload)`, never raising); the record says what happened in
    `lane_sent` / `lane_skip`."""
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def hook(row, tx, receipt, eth_usd, eth_usd_age_s, price_source, *, path="direct", verify_ms=None):
        from sniper.settings import memecoin as cfg
        try:
            rec = decide(tx, receipt, mint=(row.get("buy") or {}).get("mint") or row.get("mint"),
                         eth_usd=eth_usd, eth_usd_age_s=eth_usd_age_s,
                         clip_usd=(clip_usd_fn or lane_clip_usd)(),
                         chase_pct=cfg.MEMECOIN_REAL_CHASE_MAX_PCT,
                         premium=cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM, key_fn=key_fn)
            decided_at = clock()
            rec.update(decided_at=decided_at, tx_hash=str(row.get("tx_hash") or "").lower(),
                       wallet=row.get("wallet"), source_user_id=row.get("source_user_id"),
                       handle=row.get("handle"), detection_price_source=price_source,
                       path=path, verify_ms=verify_ms, arrival_at=row.get("now"),
                       since_arrival_ms=_since_ms(row.get("now"), decided_at))
            if mode() in ("runner_shadow", "live") and send_fn is not None:
                now = host_clock()
                age = eth_usd_age_s if eth_usd_age_s is not None else MAX_MARK_AGE_S + 1
                payload, why = lane_datagram(
                    rec, row, receipt, eth_usd=eth_usd, eth_usd_at=now - float(age), sent_epoch=now,
                    routes=cfg.MEMECOIN_BASE_FAST_ROUTES, chase_pct=cfg.MEMECOIN_REAL_CHASE_MAX_PCT,
                    premium=cfg.MEMECOIN_REAL_MAX_EXEC_PREMIUM)
                if payload is not None and payload["preconf"]:
                    # A preconfirmed source the watcher already proved a phantom is never sent
                    # (Alpha ruling 2026-10-14): one local read on this decide thread's own handle.
                    why = retracted_fn(payload)
                    payload = None if why else payload
                if payload is None:
                    rec["lane_skip"] = why
                else:
                    rec["lane_sent"] = bool(send_fn(payload))
            writer.put(rec)
            return rec
        except Exception:
            logger.exception("base fast lane: hook failed (today's path covers the signal)")
            return {"reason": "error"}
    return hook

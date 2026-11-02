"""Paper exit decisions on on-chain ticks (onchain_ticks_v1), not DexScreener mids.

Spec: docs/superpowers/specs/2026-10-08-memecoin-paper-exit-onchain-ticks-design.md (Gamma PASS,
Alpha GO, operator rulings 2026-10-08).

A study-arm paper position opened while MEMECOIN_PAPER_EXIT_ENGINE=onchain_ticks_v1 freezes
`exit_engine` into its policy; the 15 s DexScreener sweep then skips it entirely (except the 14-day
backstop) and this engine decides every rail, through the SAME rail function the sweep calls
(`memecoin_exits._frozen_real_shadow_reason`), on the pool's own state:

- 4663 / 8453: the bound pool's newest swap on our own tape (data/memecoin_onchain_volume.db,
  read-only), a V3/V4 post-trade SPOT (`v3_spot` / `v4_spot`), valued in USD at the quote token's
  price. A V2 pool has no reserves on that tape, so it gets no tick (Gamma 4 wants bag-size V2).
  The price is current while the tape writer delivered within FEED_TTL_S, the pool's protocol
  stream (V4/V3) is measured current in volume_feed_lag (review C09), and no tick gap is open: a
  concentrated-liquidity spot moves only on a swap, so a quiet pool keeps its last swap's price.
- 56: an exact whole-bag sell quote on the paper entry's own route (`bag_quote`), read by its own
  thread on the dRPC 56 research key through the account's shared pacer (BscBagQuotes).
- Solana: the paper vault stream (part b), when its source is attached.

NO TICK IS NEVER A PRICE. Missing or stale: the price rails HOLD and the seconds are counted
(blind_s beside live_s); the time rails still fire. The close is unchanged: `_close` ->
`executable_exit_fill` on the paper key. Nothing here touches a funded seam, the funded loop or a
funded key; the only ledger writes are the paper rows this engine owns.
"""
from __future__ import annotations

import collections
import contextlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from fractions import Fraction
from typing import NamedTuple

logger = logging.getLogger("memecoin")

V0, V1 = "dexscreener_sweep_v0", "onchain_ticks_v1"
V1_ARMS = ("funded_clone", "profile_matched_v1")        # memecoin_exits.STUDY_ARMS
FEED_TTL_S = 5.0
PERSIST_PEAK_S = 5.0          # a new high is written at most this often (the decision reads memory)
PERSIST_QUOTE_S = 15.0        # last_quote_price, as often as the v0 sweep wrote it
FLUSH_S = 30.0                # live_s / blind_s into paper_tick_json
SLOW_RAILS_S = 15.0           # rails that read the ledger (vol_death), as often as the v0 sweep


# FUNDED DECISION MARKS, OFF THE FUNDED EXIT THREAD (Gamma LOW-1 on b2cf62ff8, rule 2): the funded
# sweep only appends here (no I/O, no lock a writer holds); this engine's own thread and connection
# write them. Bounded: a full queue drops the newest and counts it, it never blocks the exit.
_DECISIONS = collections.deque()
_DECISIONS_MAX = 512
DECISION_COUNTS = collections.Counter()


def queue_exit_decision(position_id, value):
    """Queue one funded exit decision's mark (JSON) for the engine thread to write. Never raises,
    never blocks."""
    try:
        if len(_DECISIONS) >= _DECISIONS_MAX:
            DECISION_COUNTS["dropped"] += 1
            return False
        _DECISIONS.append((int(position_id), value))
        DECISION_COUNTS["queued"] += 1
        return True
    except Exception:
        DECISION_COUNTS["errors"] += 1
        return False


def drain_exit_decisions(conn, limit=_DECISIONS_MAX):
    """Write the queued funded decision marks on `conn` (the engine thread), one commit."""
    from sniper.persistence import db_memecoin as m
    n = 0
    while _DECISIONS and n < limit:
        pid, value = _DECISIONS.popleft()
        try:
            m.record_exit_decision(conn, pid, value, commit=False)
            n += 1
        except Exception:
            DECISION_COUNTS["errors"] += 1
            logger.exception("paper ticks: funded decision mark not written pos=%s", pid)
    if n:
        conn.commit()
        DECISION_COUNTS["written"] += n
    return n


def exit_engine(pos):
    """The engine a paper position opened under (absent = v0)."""
    from sniper.services.memecoin_policy import paper_policy_value
    return str(paper_policy_value(pos, "exit_engine", V0) or V0)


def engine_params(arm):
    """{"exit_engine": V1} for a study arm opened while the knob says v1, else {} (v0 bytes)."""
    from sniper.settings import memecoin as cfg
    if arm in V1_ARMS and str(getattr(cfg, "MEMECOIN_PAPER_EXIT_ENGINE", V0)) == V1:
        return {"exit_engine": V1}
    return {}


def entry_binding(network_id, route_binding, token_decimals=None):
    """{"binding": "entry_route", "pool", "route"?} from a paper entry quote's route binding: the
    last Jupiter hop's ammKey on Solana; the V4 pool id or the coin-side V2/V3 pool on EVM, with the
    route the 56 bag quote re-reads. None when the quote named no pool."""
    if not isinstance(route_binding, dict):
        return None
    if str(network_id) == "solana":
        keys = route_binding.get("amm_keys") or ()
        return {"binding": "entry_route", "pool": str(keys[-1])} if keys else None
    protocol = route_binding.get("protocol")
    pool = (route_binding.get("pool_id") if protocol == "uniswap_v4"
            else route_binding.get("market_pool"))
    if not pool:
        return None
    out = {"binding": "entry_route", "pool": str(pool).lower(), "route": dict(route_binding)}
    try:
        if token_decimals is not None and int(token_decimals) >= 0:
            out["coin_decimals"] = int(token_decimals)   # read by the entry quote itself
    except (TypeError, ValueError):
        pass
    return out


def binding_of(pos):
    try:
        got = json.loads(pos.get("paper_tick_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


class Tick(NamedTuple):
    price: float          # USD per coin (human units)
    at: float             # epoch the price is known current at
    source: str           # tape_v3_spot | tape_v4_spot | paper_bag_quote_56 | paper_sol_*
    tick_kind: str        # v3_spot | v4_spot | bag_quote | ...
    observed_at: float    # epoch of the swap or quote behind it
    carried: bool = False  # standing state beyond observation freshness; still a live price


def _epoch(iso):
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


class QuoteUsd:
    """USD per quote token: 1 for a configured USD stable or a USD-quoted chain, the chain's native
    price (quote_usd_price, cached TTL_S) for native or its wrapped quote, else None (no tick)."""
    TTL_S = 30.0

    def __init__(self, native_fn=None, clock=time.time):
        self._native_fn, self.clock, self._cache = native_fn, clock, {}

    def native(self, chain):
        hit = self._cache.get(chain)
        if hit is not None and self.clock() - hit[0] < self.TTL_S:
            return hit[1]
        try:
            if self._native_fn is not None:
                px = self._native_fn(chain)
            else:
                from sniper.execution.evm_wallet import quote_usd_price
                px = quote_usd_price(chain)
        except Exception:
            logger.exception("paper ticks: quote USD unavailable chain=%s", chain)
            px = None
        px = float(px) if px and float(px) > 0 else None
        self._cache[chain] = (self.clock(), px)
        return px

    def __call__(self, chain, token):
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        from sniper.settings import memecoin as cfg
        token, chain = str(token or "").lower(), str(chain)
        if token in {t.lower() for t in cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get(chain, ())}:
            return 1.0
        quote = CHAIN_QUOTE.get(chain) or {}
        if token == "0x" + "00" * 20 or token == str(quote.get("token") or "").lower():
            return 1.0 if quote.get("kind") == "usd" else self.native(chain)
        return None


def spot_usd(sqrt_price_x96, *, mint_is_c1, coin_decimals, quote_decimals, quote_usd):
    """USD per coin from a V3/V4 post-trade sqrt price; None when anything is missing."""
    try:
        sq = int(sqrt_price_x96) ** 2
        if sq <= 0 or quote_usd is None:
            return None
        per_atomic = Fraction(1 << 192, sq) if mint_is_c1 else Fraction(sq, 1 << 192)
        value = float(per_atomic * Fraction(10) ** (int(coin_decimals) - int(quote_decimals))) * float(quote_usd)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


_RAYBOT_TX = "swap:raybot:"


def _block_hash(value):
    value = value.lower() if isinstance(value, str) else ""
    return value if re.fullmatch(r"0x[0-9a-f]{64}", value) else None


class EvmTapeTicks:
    """4663 / 8453 ticks off the on-chain swap tape (read-only). `tick(tape, pos, binding, now)`
    returns (Tick or None, why); `source_pool` resolves the source-swap fallback binding."""

    def __init__(self, usd_fn):
        self.usd_fn = usd_fn

    @staticmethod
    def healthy(tape, chain, now, pool):
        """The writer's own last delivery within FEED_TTL_S, the POOL'S protocol stream measured
        current, and no open tick gap: (at, None) or (None, why).

        A FRESH HEADER IS NOT A CURRENT POOL (review C09, 2026-10-08): `received` is the header
        socket, and a V4 log stream 120 s behind it carried the pool's last swap as a current tick.
        The pool's protocol (V4 for a 32-byte pool id, V3 otherwise) must have a volume_feed_lag
        measurement within LAG_TTL_S of at most LAG_MAX_S, the rule the 4663 display marks use
        (memecoin_evm_tape_marks). No measurement, a stale one, or a store from before that table
        is unknown, never fresh. A quiet pool on a current stream still carries its last swap."""
        from sniper.services.memecoin_evm_tape_marks import LAG_MAX_S, LAG_TTL_S
        row = tape.execute("SELECT received FROM volume_feed WHERE chain_id=?", (chain,)).fetchone()
        try:
            at = float(row[0])
        except (TypeError, ValueError):
            return None, "feed_unknown"
        if not -1.0 <= now - at <= FEED_TTL_S:
            return None, "feed_stale"
        try:
            lag = tape.execute("SELECT v4_lag_s, v3_lag_s, observed FROM volume_feed_lag "
                               "WHERE chain_id=?", (chain,)).fetchone()
            value = lag[0 if len(str(pool)) == 66 else 1]
            lag_age = now - float(lag[2])
            value = None if value is None else float(value)
        except (sqlite3.OperationalError, TypeError, ValueError):
            return None, "protocol_lag_unknown"
        if (value is None or not math.isfinite(value) or value < 0
                or not -1.0 <= lag_age <= LAG_TTL_S):
            return None, "protocol_lag_unknown"
        if value > LAG_MAX_S:
            return None, "protocol_lag"
        if tape.execute("SELECT 1 FROM tick_gaps WHERE chain_id=? AND resolved_at IS NULL LIMIT 1",
                        (chain,)).fetchone():
            return None, "tick_gap"
        return at, None

    @staticmethod
    def source_pool(tape, pos):
        """The pool of the source's own swap on the tape (origin swap:raybot:<tx>:...), or None."""
        origin = str(pos.get("origin_event_id") or "").lower()
        if not origin.startswith(_RAYBOT_TX):
            return None
        tx, mint = origin[len(_RAYBOT_TX):].split(":", 1)[0], str(pos.get("mint") or "").lower()
        pools = {r[0] for r in tape.execute(
            "SELECT t.pool,t.block_hash,b.block_hash FROM pool_ticks t "
            "JOIN pool_meta m ON m.chain_id=t.chain_id AND m.pool=t.pool "
            "JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
            "AND lower(b.block_hash)=lower(t.block_hash) "
            "WHERE t.chain_id=? AND t.tx_sig=? AND (lower(m.token0)=? OR lower(m.token1)=?)",
            (str(pos.get("network_id")), tx, mint, mint)) if _block_hash(r[1]) and _block_hash(r[2])}
        return pools.pop() if len(pools) == 1 else None

    @staticmethod
    def decimals(tape, chain, token, meta_value, known=None):
        """A token's decimals: the pool's own meta, else what the entry quote read (`known`), else
        native and the chain's wrapped quote by definition, else the tape's own token table. None
        when nothing knows it (no tick: a guessed 18 would misprice a 6-decimal coin 10**12x).
        2026-10-08 live: 88% of 4663 and 99% of 8453 pool_meta rows carry no decimals."""
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        if meta_value is not None:
            return int(meta_value)
        if known is not None:
            return int(known)
        token = str(token or "").lower()
        quote = CHAIN_QUOTE.get(str(chain)) or {}
        if token == "0x" + "00" * 20:
            return 18
        if token == str(quote.get("token") or "").lower() and quote.get("decimals") is not None:
            return int(quote["decimals"])
        try:
            row = tape.execute("SELECT decimals FROM volume_token_decimals WHERE chain_id=? "
                               "AND token=?", (str(chain), token)).fetchone()
        except sqlite3.Error:
            return None
        return int(row[0]) if row and row[0] is not None else None

    def tick(self, tape, pos, pool, now, coin_decimals=None):
        chain, mint = str(pos.get("network_id")), str(pos.get("mint") or "").lower()
        at, why = self.healthy(tape, chain, now, pool)
        if at is None:
            return None, why
        try:
            rows = tape.execute(
                "SELECT t.kind,t.sqrt_price_x96,t.observed_at,t.block_hash,b.block_hash "
                "FROM pool_ticks t LEFT JOIN block_times b "
                "ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
                "WHERE t.chain_id=? AND t.pool=? AND t.kind IN ('v2_swap','v3_swap') "
                "ORDER BY t.block_no DESC,t.log_index DESC LIMIT 8", (chain, pool)).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such column" in str(exc):
                return None, "hash_unverified"
            raise
        if not rows:
            return None, "no_swap"
        row = None
        for candidate in rows:
            hashes = [_block_hash(h) for h in candidate[3:5]]
            if not all(hashes):
                return None, "hash_unverified"
            if hashes[0] == hashes[1]:
                row = candidate
                break
            # A proven orphan is excluded. An unverified newer state above cannot be skipped.
        if row is None:
            return None, "no_canonical_swap"
        observed = _epoch(row[2])
        if observed is None or not math.isfinite(observed) or observed > now + 1.0:
            return None, "observation_unknown"
        if row[0] == "v2_swap" or not row[1]:
            return None, "v2_no_reserves"
        meta = tape.execute("SELECT token0, token1, token0_decimals, token1_decimals FROM pool_meta "
                            "WHERE chain_id=? AND pool=?", (chain, pool)).fetchone()
        if not meta:
            return None, "meta_unresolved"
        token0, token1 = str(meta[0]).lower(), str(meta[1]).lower()
        if mint not in (token0, token1):
            return None, "pool_not_the_coins"
        mint_is_c1 = token1 == mint
        quote_token = token0 if mint_is_c1 else token1
        coin_dec = self.decimals(tape, chain, mint, meta[3] if mint_is_c1 else meta[2], coin_decimals)
        quote_dec = self.decimals(tape, chain, quote_token, meta[2] if mint_is_c1 else meta[3])
        if coin_dec is None or quote_dec is None:
            return None, "decimals_unknown"
        price = spot_usd(row[1], mint_is_c1=mint_is_c1, coin_decimals=coin_dec,
                         quote_decimals=quote_dec, quote_usd=self.usd_fn(chain, quote_token))
        if price is None:
            return None, "quote_unpriced"
        kind = "v4_spot" if len(pool) == 66 else "v3_spot"
        from sniper.services.memecoin_evm_tape_marks import CARRY_AFTER_S
        return Tick(price, at, "tape_" + kind, kind, observed,
                    at - observed > CARRY_AFTER_S), None


class BscBagQuotes:
    """56 (operator 2026-10-08): an exact whole-bag sell quote on each open 56 v1 position's own
    entry route, on MEMECOIN_RESEARCH_DRPC_RPC_URL_56 only, every call through the dRPC account's
    shared pacer. At most one read per position per `period_s`; nothing while none is open. A limit
    answer pauses the whole account (the pacer) and this skips, never blocking, while it lasts; a
    quota answer or a third limit inside an hour stops it for the process, paged once."""
    LIMITS_TO_STOP, LIMIT_WINDOW_S = 3, 3600.0

    def __init__(self, url, *, usd_fn, period_s, http=None, quote_fn=None, decimals_fn=None,
                 paused_fn=None, alert_fn=None, clock=time.time):
        from sniper.services.memecoin_research_pacer import drpc_paced_http
        if http is None:
            from sniper.execution.evm_wallet import _http as http
        self.url, self.usd_fn, self.period_s, self.clock = url, usd_fn, float(period_s), clock
        self.http = self._watched(drpc_paced_http(http))
        self._quote_fn, self._decimals_fn = quote_fn, decimals_fn
        self._paused_fn, self._alert_fn = paused_fn, alert_fn
        self.counters = collections.Counter()
        self.stopped = None
        self._limits = collections.deque()
        self._ticks, self._read_at, self._decimals = {}, {}, {}
        self._lock = threading.Lock()
        self.wanted = []                 # [(pid, mint, open qty, route)] set by the engine

    def _watched(self, http):
        def call(method, url, headers=None, body=None, timeout=10):
            self.counters["calls"] += 1
            try:
                out = http(method, url, headers=headers, body=body, timeout=timeout)
            except Exception as exc:
                if "429" in str(exc) or getattr(exc, "code", None) == 429:
                    self._limit()
                raise
            status, reply = out[0], out[1]
            errors = [r.get("error") for r in (reply if isinstance(reply, list) else [reply])
                      if isinstance(r, dict) and r.get("error")]
            for err in errors:
                text = str(err.get("message") if isinstance(err, dict) else err).lower()
                if (isinstance(err, dict) and err.get("code") == -32001) or "usage limit" in text:
                    self._stop("quota")
                elif "rate limit" in text or (isinstance(err, dict) and err.get("code") == 15):
                    self._limit()
            if status == 429 and not errors:
                self._limit()
            return out
        return call

    def _limit(self):
        now = self.clock()
        self.counters["limits"] += 1
        self._limits.append(now)
        while self._limits and now - self._limits[0] > self.LIMIT_WINDOW_S:
            self._limits.popleft()
        if len(self._limits) >= self.LIMITS_TO_STOP:
            self._stop("limits")

    def _stop(self, why):
        if self.stopped is not None:
            return
        self.stopped = why
        logger.error("paper ticks 56: poller stopped for the process (%s); 56 price rails hold", why)
        try:
            if self._alert_fn is not None:
                self._alert_fn("paper 56 tick poller stopped (%s): 56 v1 price rails hold until a "
                               "restart; the dRPC 56 account is shared with the hop seeds." % why)
        except Exception:
            logger.exception("paper ticks 56: page failed")

    def _paused(self):
        try:
            if self._paused_fn is not None:
                return self._paused_fn(self.url)
            from sniper.services.memecoin_research_pacer import drpc_paused_s
            return drpc_paused_s(self.url)
        except Exception:
            return 0.0

    def tick(self, pid):
        with self._lock:
            return self._ticks.get(int(pid))

    def forget(self, live_ids):
        with self._lock:
            for pid in [p for p in self._ticks if p not in live_ids]:
                self._ticks.pop(pid, None)
                self._read_at.pop(pid, None)

    def step(self):
        """One pass over the wanted positions (its own thread): reads the due ones."""
        if self.stopped is not None:
            return
        for pid, mint, qty, route in list(self.wanted):
            if self.stopped is not None:
                return
            now = self.clock()
            if now - self._read_at.get(pid, -1e18) < self.period_s:
                continue
            if self._paused() > 0:
                self.counters["pause_skips"] += 1
                return                                      # the whole account waits; so do we
            self._read_at[pid] = now
            try:
                tick = self._read(mint, qty, route)
            except Exception:
                logger.exception("paper ticks 56: bag quote failed pos=%s", pid)
                tick = None
            self.counters["reads"] += 1
            if tick is None:
                self.counters["no_quote"] += 1
                continue
            with self._lock:
                self._ticks[pid] = tick

    def _read(self, mint, qty, route):
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        quote = CHAIN_QUOTE["56"]
        dec = self._decimals.get(mint)
        if dec is None:
            if self._decimals_fn is not None:
                dec = self._decimals_fn(mint)
            else:
                from sniper.execution.evm_wallet import erc20_decimals
                dec = erc20_decimals(mint, self.url, http=self.http)
            if dec is None:
                return None
            self._decimals[mint] = dec = int(dec)
        qty_atomic = int(Fraction(str(qty)) * 10 ** dec)
        if qty_atomic <= 0:
            return None
        if self._quote_fn is not None:
            out = self._quote_fn(mint, qty_atomic, route)
        else:
            from sniper.services.memecoin_quotes import PAPER_QUOTE_TAKER, _evm_bound_amount_out
            out = _evm_bound_amount_out(56, mint, quote["token"], qty_atomic, route=route,
                                        taker=PAPER_QUOTE_TAKER, rpc_url=self.url, http=self.http)
        got = int((out or {}).get("amount_out") or 0)
        usd = self.usd_fn("56", quote["token"])
        if got <= 0 or not usd:
            return None
        price = got / 10 ** int(quote["decimals"]) * float(usd) / float(qty)
        if not math.isfinite(price) or price <= 0:
            return None
        now = self.clock()
        return Tick(price, now, "paper_bag_quote_56", "bag_quote", now)

    def heartbeat(self):
        return dict(self.counters, stopped=self.stopped, wanted=len(self.wanted))


def _stored_seconds(got, key):
    """A persisted coverage total: finite and >= 0, else 0 (malformed or absent is no evidence)."""
    try:
        value = float(got.get(key))
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value >= 0 else 0.0


def _restored_state(stored, now, opened_at=None):
    """A position's tick state on this engine's first pass (review C02, 2026-10-08). The coverage
    totals are LIFETIME totals, so they are restored, never reset. The time since the last
    accounted instant is BLIND: nothing observed the position while no engine ran, and a price
    available after the restart is not evidence of continuity. A stored `accounted_at` in the
    future (clock reversal) adds nothing. A row without one (written before this change) adds no
    downtime: its history before this deploy is already lost and is not invented. A row with no
    accounting at all (no accounted_at, every total 0) is new: the time from its opening to this
    first visit is blind too (Gamma LOW: unbounded when the engine thread died alone)."""
    blind = _stored_seconds(stored, "blind_s")
    accounted = _stored_seconds(stored, "accounted_at")
    if accounted <= 0 and opened_at and not any(
            _stored_seconds(stored, k) for k in ("live_s", "blind_s", "carried_s")):
        try:
            from datetime import datetime
            accounted = datetime.fromisoformat(str(opened_at)).timestamp()
        except (TypeError, ValueError):
            accounted = 0.0
    if accounted > 0:
        blind += max(0.0, now - accounted)
    return {"last": now, "since": now, "live_s": _stored_seconds(stored, "live_s"), "blind_s": blind,
            "carried_s": _stored_seconds(stored, "carried_s"), "hits": 0, "peak_at": 0.0,
            "quote_at": 0.0, "slow_at": 0.0, "flushed": now, "why": None}


class PaperTickEngine:
    """The v1 decision loop (`memecoin-paper-ticks`, its own thread and ledger connection)."""

    def __init__(self, *, connect, now_fn, tape_path=None, evm=None, bsc=None, sol=None,
                 clock=time.time, max_age_s=None, bsc_period_s=None):
        from sniper.settings import memecoin as cfg
        self.connect, self.now_fn, self.clock = connect, now_fn, clock
        self.tape_path = tape_path
        self.evm, self.bsc, self.sol = evm, bsc, sol
        self.max_age_s = float(cfg.MEMECOIN_PAPER_TICK_MAX_AGE_S if max_age_s is None else max_age_s)
        self.bsc_age_s = self.max_age_s + float(
            cfg.MEMECOIN_PAPER_TICK_56_PERIOD_S if bsc_period_s is None else bsc_period_s)
        self.counters = collections.Counter()
        self._state = {}
        self._due = []
        self._conn = None

    def positions(self, conn):
        from sniper.persistence import db_memecoin as m
        out = []
        for arm in V1_ARMS:
            out += [p for p in m.open_positions_by_strategy(conn, arm) if exit_engine(p) == V1]
        return out

    def _tape(self):
        if not self.tape_path:
            return contextlib.nullcontext(None)
        return contextlib.closing(sqlite3.connect("file:%s?mode=ro" % self.tape_path, uri=True,
                                                  timeout=0.5, isolation_level=None))

    def step(self):
        if self._conn is None:
            self._conn = self.connect()
        conn, now, now_iso = self._conn, self.clock(), self.now_fn()
        drain_exit_decisions(conn)
        positions = self.positions(conn)
        live = {int(p["id"]) for p in positions}
        if self.bsc is not None:
            from sniper.persistence import db_memecoin as m
            self.bsc.wanted = [
                (int(p["id"]), str(p["mint"]).lower(), m.open_qty(conn, p["id"]),
                 binding_of(p).get("route")) for p in positions
                if str(p.get("network_id")) == "56" and binding_of(p).get("route")]
            self.bsc.forget(live)
        if self.sol is not None:
            self.sol.wanted = [
                (int(p["id"]), str(p["mint"]), binding_of(p).get("pool"), p.get("origin_event_id"))
                for p in positions if str(p.get("network_id")) == "solana"]
        # AN UNAVAILABLE TAPE IS A STATE OF THIS PASS, NOT A SKIPPED PASS (review C01, 2026-10-08).
        # Opening it around the whole loop meant a missing or unreadable file visited no position:
        # no time rail fired, and Solana/BSC positions, which never read the tape, stopped too.
        self._tape_read_logged = False
        try:
            tape_ctx = self._tape()
            tape = tape_ctx.__enter__()
        except sqlite3.Error:
            tape_ctx, tape = None, None
            self.counters["tape_errors"] += 1
            logger.exception("paper ticks: tape unavailable (EVM price rails hold this pass)")
        self._due = []
        try:
            for pos in positions:
                try:
                    self._one(conn, tape, pos, now, now_iso)
                except Exception:
                    self.counters["errors"] += 1
                    logger.exception("paper ticks: position %s failed", pos.get("id"))
        finally:
            if tape_ctx is not None:
                tape_ctx.__exit__(None, None, None)
        self._record_ticks(conn, now_iso)
        for pid in [p for p in self._state if p not in live]:
            self._state.pop(pid, None)
        # WHAT A READER MUST CENSOR (Gamma): open v1 positions by chain, binding and hold reason.
        self.book = collections.Counter(
            "%s|%s|%s" % (p.get("network_id"),
                          binding_of(p).get("binding")
                          or (self._state.get(int(p["id"]), {}).get("binding") or {}).get("binding")
                          or "unbound",
                          self._state.get(int(p["id"]), {}).get("why") or "live")
            for p in positions)

    def _evm_tick(self, tape, pos, binding, st, now):
        pool = binding.get("pool")
        if not pool and not st.get("unbound"):
            pool = self.evm.source_pool(tape, pos)
            if pool:
                st["binding"] = {"binding": "source_swap", "pool": pool}
            elif now - st["since"] > 600:
                st["unbound"] = True             # the tape keeps ~1 h; give up after 10 min
        pool = binding.get("pool") or (st.get("binding") or {}).get("pool")
        if not pool:
            return None, "unbound"
        return self.evm.tick(tape, pos, pool, now, coin_decimals=binding.get("coin_decimals"))

    def _tick(self, tape, pos, binding, st, now):
        chain = str(pos.get("network_id"))
        if chain == "56":
            if self.bsc is None:
                return None, "no_56_source"
            return self.bsc.tick(pos["id"]), ("bsc_stopped" if self.bsc.stopped else "no_quote_yet")
        if chain in ("4663", "8453"):
            if self.evm is None or tape is None:
                return None, "no_tape"
            try:
                return self._evm_tick(tape, pos, binding, st, now)
            except sqlite3.Error:
                # A tape that opened but cannot be read (corrupt, locked past its timeout): this
                # position's price holds, and its time rails still run in _one (review C01).
                self.counters["tape_errors"] += 1
                if not getattr(self, "_tape_read_logged", False):   # one traceback per pass (Gamma)
                    self._tape_read_logged = True
                    logger.exception("paper ticks: tape read failed position=%s (once per pass)",
                                     pos.get("id"))
                return None, "tape_error"
        if chain == "solana":
            if self.sol is None:
                return None, "no_sol_source"
            got = self.sol.binding(pos["id"])
            if got:
                st["binding"] = got              # what the stream actually bound (entry/source/graduated)
            return self.sol.tick(pos, binding, now)
        return None, "chain_unsupported"

    def _one(self, conn, tape, pos, now, now_iso):
        from sniper.persistence import db_memecoin as m
        from sniper.services.memecoin_exits import (
            _close, _frozen_real_shadow_reason, _shadow_derisk)
        from sniper.services.memecoin_policy import paper_policy_value
        from sniper.settings import memecoin as S
        pid = int(pos["id"])
        binding = binding_of(pos)
        if pid not in self._state:
            self._state[pid] = _restored_state(binding, now, pos.get("opened_at"))
        st = self._state[pid]
        tick, why = self._tick(tape, pos, binding, st, now)
        age_s = self.bsc_age_s if str(pos.get("network_id")) == "56" else self.max_age_s
        fresh = tick is not None and -1.0 <= now - tick.at <= age_s
        dt, st["last"] = max(0.0, now - st["last"]), now
        carried = fresh and (tick.carried or str(tick.tick_kind).endswith("_carried"))
        st["live_s" if fresh else "blind_s"] += dt
        if carried:
            st["carried_s"] = st.get("carried_s", 0.0) + dt   # live, but a carried quiet-pool spot
        st["why"] = None if fresh else (why or "stale")
        self.counters[("live:carried" if carried else "live") if fresh else "hold:%s" % st["why"]] += 1
        mid = tick.price if fresh else None
        # THE PEAK IN MEMORY, WRITTEN AT MOST EVERY PERSIST_PEAK_S (and before any close): the
        # trail reads it every tick, the ledger does not need a write per tick.
        db_peak = float(pos.get("peak_price") or pos.get("entry_price") or 0.0)
        if mid and mid > max(db_peak, st.get("peak") or 0.0):
            st["peak"], st["peak_ts"] = mid, now_iso
        dirty = bool(st.get("peak")) and st["peak"] > db_peak
        if dirty:
            pos["peak_price"], pos["peak_ts"] = st["peak"], st["peak_ts"]
            if now - st["peak_at"] >= PERSIST_PEAK_S:
                self._save_peak(conn, pos, st, now)
                dirty = False
        if mid and now - st["quote_at"] >= PERSIST_QUOTE_S:
            m.update_position_last_quote(conn, pid, mid, now_iso)
            st["quote_at"] = now
            # PAPER STEP 1 (spec 2026-10-08 paper path without DexScreener): the same point is the
            # paper mark and the paper path point, written once for the pass in `_record_ticks`.
            self._due.append((pos, {"price": mid, "source": "paper_tick:%s" % tick.tick_kind}))
        slow = now - st["slow_at"] >= SLOW_RAILS_S
        if slow:
            st["slow_at"] = now
        reason = _frozen_real_shadow_reason(pos, mid, now_iso, conn=conn if slow else None,
                                            unquotable=False)
        if reason in ("b_stop", "catastrophe"):
            st["hits"] += 1
            needed = int(paper_policy_value(
                pos, "stop_debounce_sweeps", S.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS))
            if st["hits"] < needed:
                reason = None
        else:
            st["hits"] = 0
        if slow or now - st["flushed"] >= FLUSH_S:
            self._flush(conn, pos, binding, st, now, tick if fresh else None)
        if not reason:
            return
        if dirty:
            self._save_peak(conn, pos, st, now)
        if reason == "derisk":
            done = _shadow_derisk(conn, pos, mid, None, now_iso)
        else:
            done = _close(conn, pos, mid, None, now_iso, reason,
                          source=tick.source if fresh else "paper_tick_none", check_plausible=fresh)
        if not done:
            return
        self.counters["decided:%s" % reason] += 1
        if reason == "derisk":
            return                          # a partial: the position stays open and decides again
        self._flush(conn, pos, binding, st, now, tick if fresh else None)
        decision = {"engine": V1, "reason": reason, "price": mid,
                    "source": tick.source if fresh else None,
                    "tick_kind": tick.tick_kind if fresh else None,
                    "age_ms": round((now - tick.at) * 1000.0, 1) if fresh else None,
                    "observed_age_ms": (round((now - tick.observed_at) * 1000.0, 1)
                                        if fresh else None),
                    "hold_why": st["why"]}
        try:
            m.record_exit_decision(conn, pid, json.dumps(decision, sort_keys=True))
        except Exception:
            logger.exception("paper ticks: decision record failed pos=%s", pid)

    def _record_ticks(self, conn, now_iso):
        """This pass's due ticks onto the mark tape and the paper path: one write each for the whole
        pass, not one per position (the ledger's write lock is shared with the funded runner).
        Log-only: a failed write never stops the loop, and no tick means no row, never a mid."""
        due, self._due = self._due, []
        if not due:
            return
        from sniper.execution.solana_quote import quote_key
        from sniper.services import memecoin_exits as EX
        from sniper.services.memecoin_paper_path import BASIS_TICK, record_paper_path
        try:
            EX.write_marks(conn, {quote_key(pos.get("network_id"), pos["mint"]): point
                                  for pos, point in due}, now_iso)
        except Exception:
            logger.exception("paper ticks: tick marks not written")
        try:
            record_paper_path(conn, [pos for pos, _ in due],
                              {int(pos["id"]): point for pos, point in due}, now_iso,
                              basis=BASIS_TICK)
        except Exception:
            logger.exception("paper ticks: tick path not written")

    @staticmethod
    def _save_peak(conn, pos, st, now):
        from sniper.persistence import db_memecoin as m
        m.update_position_peak(conn, int(pos["id"]), st["peak"], st.get("peak_ts"),
                               pos.get("no_new_high_sweeps") or 0)
        st["peak_at"] = now

    @staticmethod
    def _flush(conn, pos, binding, st, now, tick):
        from sniper.persistence import db_memecoin as m
        out = dict(binding or {"binding": "unbound"})
        if st.get("binding"):
            if binding.get("pool") and st["binding"].get("pool") != binding.get("pool"):
                out["entry_pool"] = binding.get("pool")
            out.update(st["binding"])
        out.update(live_s=round(st["live_s"], 1), blind_s=round(st["blind_s"], 1),
                   carried_s=round(st.get("carried_s", 0.0), 1), accounted_at=round(st["last"], 3),
                   hold_why=st["why"], tick_kind=tick.tick_kind if tick else out.get("tick_kind"))
        try:
            m.set_paper_tick_json(conn, int(pos["id"]), json.dumps(out, sort_keys=True))
        except Exception:
            logger.exception("paper ticks: hold accounting not written pos=%s", pos.get("id"))
        st["flushed"] = now

    def write_heartbeat(self, path):
        """The engine's own heartbeat file (Gamma: the 56 poller's calls, limits, pause skips and
        stop state measurable beside the account's other users), once a minute."""
        from sniper.util.atomic import atomic_write_json
        from sniper.util.clock import real_utc_now_iso
        try:
            atomic_write_json(path, dict(self.heartbeat(), at=real_utc_now_iso()))
        except Exception:
            logger.exception("paper ticks: heartbeat not written")

    def heartbeat(self):
        out = {"counters": dict(self.counters), "open": len(self._state),
               "book": dict(getattr(self, "book", {})), "funded_decisions": dict(DECISION_COUNTS),
               "funded_decisions_queued": len(_DECISIONS)}
        if self.bsc is not None:
            out["bsc"] = self.bsc.heartbeat()
        if self.sol is not None and hasattr(self.sol, "heartbeat"):
            out["sol"] = self.sol.heartbeat()
        return out


_RUNNING = {}


def start_exit_decision_writer(*, connect=None, period_s=1.0, sleep=time.sleep):
    """Drain funded marks without starting paper engines or their provider consumers."""
    if "decision_writer" in _RUNNING:
        return _RUNNING["decision_writer"]
    if connect is None:
        from sniper.persistence.db_core import get_connection as connect

    def run():
        conn = None
        while True:
            try:
                if conn is None:
                    conn = connect()
                drain_exit_decisions(conn)
                conn.rollback()  # The drain swallows SQL errors, including zero-write units.
            except Exception:
                logger.exception("funded decision mark writer failed; reconnecting")
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            sleep(period_s)

    thread = threading.Thread(target=run, name="memecoin-funded-decision-marks", daemon=True)
    thread.start()
    _RUNNING["decision_writer"] = thread
    return thread


def start(*, now_fn, connect=None, env=None, period_s=1.0, sleep=time.sleep):
    """Start the engine thread (and the 56 poller thread when its key is set) once per process.
    Returns the engine. The loop runs while any v1 position may exist; with none open each pass is
    one indexed read."""
    import os
    from sniper.util.state_paths import state_path
    if "engine" in _RUNNING:
        return _RUNNING["engine"]
    env = os.environ if env is None else env
    if connect is None:
        from sniper.persistence.db_core import get_connection as connect
    usd = QuoteUsd()
    from sniper.services import memecoin_paper_sol_ticks
    sol = memecoin_paper_sol_ticks.start(env)
    bsc = None
    url = str(env.get("MEMECOIN_RESEARCH_DRPC_RPC_URL_56") or "").strip()
    if url:
        def page(text):
            from sniper.services.memecoin_alerts import send_alert
            send_alert("PAPER-TICKS-56", text)
        from sniper.settings import memecoin as cfg
        bsc = BscBagQuotes(url, usd_fn=usd, period_s=cfg.MEMECOIN_PAPER_TICK_56_PERIOD_S,
                           alert_fn=page)
    engine = PaperTickEngine(connect=connect, now_fn=now_fn, evm=EvmTapeTicks(usd), bsc=bsc, sol=sol,
                             tape_path=str(state_path("data", "memecoin_onchain_volume.db")))

    beat_path = str(state_path("data", "memecoin_paper_ticks_heartbeat.json"))

    def run(step, beat=False):
        last = 0.0
        while True:
            try:
                step()
            except Exception:
                logger.exception("paper ticks: pass failed")
            if beat and time.monotonic() - last >= 60.0:
                last = time.monotonic()
                engine.write_heartbeat(beat_path)
            sleep(period_s)
    threading.Thread(target=run, args=(engine.step, True), name="memecoin-paper-ticks",
                     daemon=True).start()
    if bsc is not None:
        threading.Thread(target=run, args=(bsc.step,), name="memecoin-paper-ticks-56",
                         daemon=True).start()
    _RUNNING["engine"] = engine
    return engine

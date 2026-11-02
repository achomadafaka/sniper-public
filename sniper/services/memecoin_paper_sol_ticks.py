"""Solana ticks for the onchain_ticks_v1 paper exit engine (spec 2026-10-08 paper exit ticks, part b).

THE PAPER VAULT STREAM. The funded swap-mark lane's own pieces (VaultTape, CurveTape, the STATE-mark
corroboration band, the curve pricing, `run_price_stream_epoch`) in a SECOND instance: its own
SolanaSwapMarkCache per stream pool (PoolMarks: the pool is the mark's identity), its own WebSocket, its own seeds and binds, all on MEMECOIN_PAPER_TICKS_HELIUS_URL
(operator 2026-10-08: the second research key, dea05c94, continuously, as an exception to its
flat-book rule). It never touches the funded cache, the funded read endpoint, the funded Helius keys
or the shared Solana tape DB; its bindings live in memory and in the paper rows' paper_tick_json.

A tick is the funded kind: a swap mark (the trade's own execution price) no older than the engine's
max age, a corroborated STATE mark, or a curve's own reserves (a curve mark holds while its
subscription is live, as funded). Price in USD at the cached on-chain quote price (fresh_quote_usd:
cache only, fail-closed per asset).

BINDING (Gamma 3): the paper quote's own last-hop pool (`entry_route`), checked against the source
swap's own transaction first (one getTransaction; the same pool means no further read), else
resolved from that pool's recent transactions (at most `MAX_FETCH` reads); the source swap's pool
when the entry pool will not resolve (`source_swap`); else `unbound`, which holds for the position's
life. A curve that completes is rebound to its canonical PumpSwap pool (`graduated`); until the
rebind lands the position holds.

KEY GUARD: the stream refuses to start when MEMECOIN_PAPER_TICKS_HELIUS_URL is unset, keyless, or
shares its key with any other configured URL but the research list it came from (every funded key,
the watcher's, option C's deposit key e1f95763, the capture pool), logged by key id only.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from datetime import datetime, timezone

from sniper.services.memecoin_paper_ticks import Tick

logger = logging.getLogger("memecoin")

KNOB = "MEMECOIN_PAPER_TICKS_HELIUS_URL"
OPTION_C_KEY = "e1f95763"
EPOCH_S = 300.0                 # the paper epoch (funded uses 30 s): one seed per account per epoch
# Keepalive on the paper socket: a dead socket closes within ~4 s, the epoch returns and the cache is
# invalidated, so a carried price never outlives the socket by more than that (Gamma, liveness).
PING_S = 2.0
MAX_FETCH = 4                   # entry-pool resolution: one signatures page and at most this many txs
MAX_ATTEMPTS = 3                # a binding that keeps failing stops spending reads
# A GRADUATED CURVE'S POOL MAY NOT RESOLVE AT ONCE (review C03, 2026-10-08): propagation lag or a
# failed read. Retry from the resolver loop at this pace until this long after the completion, then
# end in 'graduation_unresolved'. About 18 resolves of one signatures page each, paper key only.
GRAD_RETRY_EVERY_S = 10.0
GRAD_RETRY_FOR_S = 180.0
_ALLOWED_SHARERS = (KNOB, "MEMECOIN_RESEARCH_HELIUS_RPC_URLS")


def key_refusal(url, env, allowed=_ALLOWED_SHARERS):
    """Why `url` may not carry the paper stream, or None. `allowed` names the variables that may
    hold the same key (the knob itself, and for the paper stream the research list it came from)."""
    from sniper.execution.solana_rpc_pool import key_id
    url = str(url or "").strip()
    if not url:
        return "unset"
    if "api-key=" not in url:
        return "keyless"                  # never the public endpoint the funded read lane uses
    kid = key_id(url)
    if kid == OPTION_C_KEY:
        return "option_c_key"
    for name, value in sorted(env.items()):
        if name in allowed:
            continue
        for part in str(value or "").split(","):
            if "api-key=" in part and key_id(part.strip()) == kid:
                return "shared_with:%s" % name
    return None


def _epoch(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def source_signature(origin):
    """The source swap's signature from a `swap:raybot:<sig>:...` origin, else None."""
    parts = str(origin or "").split(":")
    return parts[2] if len(parts) >= 3 and parts[0] == "swap" and parts[1] == "raybot" else None


def _cpmm_programs():
    from sniper.services.memecoin_live_marks import CONSTANT_PRODUCT_PROGRAMS
    return CONSTANT_PRODUCT_PROGRAMS


class PoolMarks:
    """One funded-semantics SolanaSwapMarkCache PER STREAM POOL (engineering review C04, 2026-10-08).
    The funded cache keeps one mark per mint, so two paper positions on the same mint bound to
    different pools both took the newest print of EITHER pool, and one pool's swap corroborated the
    other's state mark. Here each pool's rows reach only that pool's cache and a tick reads only its
    bound pool: a pool without its own mark is unknown, never another pool's. The funded class is
    used unchanged. `run_price_stream_epoch` calls record, invalidate and invalidate_prices."""

    def __init__(self, on_record=None):
        self._lock = threading.Lock()
        self._pools = {}
        self._on_record = on_record

    def _for(self, pool):
        from sniper.services.memecoin_solana_swap_marks import SolanaSwapMarkCache
        with self._lock:
            got = self._pools.get(pool)
            if got is None:
                got = self._pools[pool] = SolanaSwapMarkCache()
            return got

    def get(self, pool):
        with self._lock:
            return self._pools.get(pool)

    def pools(self):
        with self._lock:
            return set(self._pools)

    def keep(self, pools):
        """Drop the caches of pools no binding names (closed or rebound positions)."""
        with self._lock:
            for pool in [p for p in self._pools if p not in pools]:
                self._pools.pop(pool, None)

    def _all(self):
        with self._lock:
            return list(self._pools.values())

    def record(self, row, binding, *, now_ts, observed_at):
        if self._on_record is not None:
            self._on_record(row)
        return self._for(binding.pool).record(row, binding, now_ts=now_ts, observed_at=observed_at)

    def invalidate(self):
        for cache in self._all():
            cache.invalidate()

    def invalidate_prices(self, now_ts=None):
        for cache in self._all():
            cache.invalidate_prices(now_ts=now_ts)

    def quote_mints(self):
        out = set()
        for cache in self._all():
            out |= cache.quote_mints()
        return out

    def fresh_price_map(self, pool, quote_usd, *, now_ts, max_age_s):
        """The funded `fresh_price_map` of `pool`'s own cache; {} when the pool has none."""
        cache = self.get(pool)
        return {} if cache is None else cache.fresh_price_map(quote_usd, now_ts=now_ts, max_age_s=max_age_s)


def stream_pool(entry):
    """The pool key a binding streams under: a curve's own account, else the pool (as `book`)."""
    return entry["quote_vault"] if entry["venue_kind"] == "pump_curve" else entry["pool"]


class PaperSolTicks:
    """The Solana tick source the engine asks: `tick(pos, binding, now)`, `binding(pid)`. Runs a
    resolver thread and a stream thread once `start()`ed; `wanted` is set by the engine."""

    def __init__(self, url, *, rpc_call=None, stream_fn=None, clock=time.time,
                 mono=time.monotonic, now_iso_fn=None, quote_usd_fn=None):
        self.url, self.clock, self.mono = url, clock, mono
        self.counters = collections.Counter()
        if rpc_call is None:
            from sniper.services.memecoin_solana_blocktime import _rpc_call, build_read_rpc_call
            # One cadence across resolver and seed threads, on the isolated paper key only.
            rpc_call = build_read_rpc_call(raw_call=lambda method, params:
                                          _rpc_call(url, method, params, timeout=10))
        self._rpc_call, self._stream_fn = rpc_call, stream_fn
        self._now_iso_fn, self._quote_usd_fn = now_iso_fn, quote_usd_fn
        self.cache = PoolMarks(on_record=self._on_row)
        self._lock = threading.Lock()
        self._bindings = {}          # pid -> {"binding", "pool", "coin_vault", "quote_vault", ...}
        self._attempts = collections.Counter()
        self._graduating = set()     # curve addresses that completed
        self.wanted = []             # [(pid, mint, entry pool or None, origin)] from the engine
        self._fresh = True           # the stream must pick up a binding change
        self._prices = (None, {})    # (mono second, {pool: price map}): one map per pool per pass
        self._socket_live = False    # inside a running epoch (the keepalive closes a dead one)
        # RULE 2, MEASURED BEFORE THE FLIP (Gamma): this stream shares the funded runner's GIL with
        # the slot lane and the funded swap marks, so its threads' CPU and its frame rate go in the
        # heartbeat as rates since the previous beat.
        self._idents = {}
        self._last_beat = None       # (monotonic, {thread: cpu s}, rows)

    def _on_row(self, row):
        self.counters["rows"] += 1
        if row.get("kind") == "sol_curve_state" and row.get("complete"):
            self._graduated(str(row.get("pool") or ""))

    # ---- RPC on the paper key only ----
    def rpc(self, method, params):
        self.counters["rpc:%s" % method] += 1
        try:
            return self._rpc_call(method, params)
        except Exception as exc:
            self.counters["rpc_errors"] += 1
            if "429" in str(exc):
                self.counters["rpc_429"] += 1
            raise

    def _get_tx(self, sig):
        from sniper.services.memecoin_solana_amounts import get_transaction_fn
        return get_transaction_fn(call=self.rpc, commitment="confirmed")(sig)

    # ---- binding ----
    def _resolve(self, mint, pool, origin):
        """(binding kind, resolved accounts) or (None, None). Reads only the paper key."""
        from sniper.services.memecoin_solana_tape import _resolve_accounts
        sig = source_signature(origin)
        source = None
        if sig:
            tx = self._get_tx(sig)
            keys = (((tx or {}).get("transaction") or {}).get("message") or {}).get("accountKeys") or []
            payer = keys[0].get("pubkey") if keys and isinstance(keys[0], dict) else (keys[0] if keys else None)
            source = _resolve_accounts(tx, mint, exclude_owner=payer) if tx else None
            if source and pool and str(source[0]) == str(pool):
                return "entry_route", source            # the source traded the pool we priced on
        read_failed = False
        if pool:
            try:
                rows = self.rpc("getSignaturesForAddress", [pool, {"limit": MAX_FETCH}]) or []
            except Exception:
                rows, read_failed = [], True        # a failed read, not an empty pool (Gamma, C03)
            for row in rows[:MAX_FETCH]:
                tx = self._get_tx(row.get("signature")) if isinstance(row, dict) else None
                # get_transaction_fn swallows a failed read into None, and a LISTED signature
                # exists, so None here is a failed read, not an absent tx (Gamma, C03).
                read_failed = read_failed or (tx is None and isinstance(row, dict))
                got = _resolve_accounts(tx, mint, required_owner=pool) if tx else None
                if got:
                    return "entry_route", got
        if source:
            return "source_swap", source
        return ("read_failed" if read_failed else None), None

    def _program(self, account):
        """The owner program of a pool account (one getAccountInfo, data-sliced to nothing), or None."""
        try:
            got = self.rpc("getAccountInfo", [str(account), {"encoding": "base64",
                                                             "dataSlice": {"offset": 0, "length": 0}}])
            return str(((got or {}).get("value") or {}).get("owner") or "") or None
        except Exception:
            self.counters["program_unread"] += 1        # permanent per binding: no carry for it
            return None

    def _classify(self, entry, account):
        """CARRY ONLY ON A KNOWN CONSTANT-PRODUCT POOL (Gamma (b)): its program by the pool account,
        the entry quote's own ammKey (or, for a source-swap binding, the vaults' owner, which is the
        pool for PumpSwap and a shared authority for Raydium, so those stay unknown)."""
        if entry["venue_kind"] != "amm":
            return entry
        program = self._program(account) if account else None
        entry["program"] = _cpmm_programs().get(program) or (("other:" + program) if program else None)
        return entry

    @staticmethod
    def _entry(kind, mint, resolved):
        from sniper.services.memecoin_solana_tape import NATIVE_SOL
        owner, coin_vault, quote_mint, quote_vault = resolved
        return {"binding": kind, "mint": mint, "pool": str(owner or coin_vault),
                "coin_vault": str(coin_vault), "quote_vault": str(quote_vault),
                "quote_mint": str(quote_mint),
                "venue_kind": "pump_curve" if quote_mint == NATIVE_SOL else "amm"}

    def _graduated(self, curve):
        with self._lock:
            self._graduating.add(curve)

    def resolve_pass(self):
        """One resolver pass (its own thread): bind what is unbound, rebind what graduated."""
        from sniper.services.memecoin_slot_graduation import canonical_pool
        with self._lock:
            wanted = list(self.wanted)
            graduating, self._graduating = set(self._graduating), set()
        live = {w[0] for w in wanted}
        for pid, mint, pool, origin in wanted:
            with self._lock:
                bound = self._bindings.get(pid)
            if bound and bound["venue_kind"] == "pump_curve" and bound["quote_vault"] in graduating:
                self.counters["graduations"] += 1
                bound = dict(bound, binding="graduating", stale=True, grad_at=self.mono(),
                             grad_next=self.mono())
                with self._lock:
                    self._bindings[pid] = bound
            if bound and bound["binding"] == "graduating":
                if self.mono() >= bound["grad_next"]:
                    self._rebind_graduated(pid, mint, bound, canonical_pool)
                continue
            if bound or self._attempts[pid] >= MAX_ATTEMPTS:
                continue
            self._attempts[pid] += 1
            try:
                kind, got = self._resolve(mint, pool, origin)
            except Exception:
                logger.exception("paper sol ticks: bind failed pos=%s", pid)
                kind, got = None, None
            if got:
                self.counters["bound:%s" % kind] += 1
                entry = self._classify(self._entry(kind, mint, got),
                                       pool if kind == "entry_route" else got[0])
                with self._lock:
                    self._bindings[pid] = entry
                    self._fresh = True
            elif self._attempts[pid] >= MAX_ATTEMPTS:
                self.counters["unbound"] += 1
        with self._lock:
            for pid in [p for p in self._bindings if p not in live]:
                self._bindings.pop(pid, None)
                self._fresh = True
            for pid in [p for p in self._attempts if p not in live]:
                self._attempts.pop(pid, None)

    def _rebind_graduated(self, pid, mint, bound, canonical_pool):
        """One retry of a pending graduation (review C03). A hit rebinds to the canonical pool. A
        miss ('not_found') and a failed read ('unreadable') keep the curve identity and graduation
        time and retry after GRAD_RETRY_EVERY_S; GRAD_RETRY_FOR_S after the completion the binding
        ends 'graduation_unresolved' and spends no more reads."""
        try:
            kind, got = self._resolve(mint, canonical_pool(mint), None)
            last = "unreadable" if kind == "read_failed" else "not_found"
        except Exception:
            logger.exception("paper sol ticks: graduated pool read failed pos=%s", pid)
            got, last = None, "unreadable"
        if got:
            self.counters["rebinds"] += 1
            entry = self._entry("graduated", mint, got)
            if entry["venue_kind"] == "amm":
                entry["program"] = "pumpswap"        # the canonical pool, by derivation
        else:
            self.counters["grad_retry:%s" % last] += 1
            now = self.mono()
            entry = dict(bound, grad_last=last, grad_next=now + GRAD_RETRY_EVERY_S)
            if now - bound["grad_at"] >= GRAD_RETRY_FOR_S:
                entry["binding"] = "graduation_unresolved"
                self.counters["grad_unresolved"] += 1
        with self._lock:
            if pid in self._bindings:                # closed while the read ran: stays dropped
                self._bindings[pid] = entry
                self._fresh = True

    def binding(self, pid):
        with self._lock:
            got = self._bindings.get(int(pid))
        if got:
            return {"binding": got["binding"], "pool": got["pool"], "program": got.get("program")}
        return {"binding": "unbound"} if self._attempts.get(int(pid), 0) >= MAX_ATTEMPTS else None

    def book(self):
        """(vaults, bindings) for the stream from the live paper bindings."""
        from sniper.services.memecoin_solana_swap_marks import PoolBinding
        from sniper.services.memecoin_solana_ws import CurveTape, VaultTape
        from sniper.services.memecoin_solana_amounts import WSOL
        vaults, bindings = {}, {}
        with self._lock:
            entries = [b for b in self._bindings.values() if not b.get("stale")]
            self._fresh = False
        for b in entries:
            if b["venue_kind"] == "pump_curve":
                curve = b["quote_vault"]
                vaults[curve] = CurveTape(curve, mint=curve)
                bindings[curve] = PoolBinding(mint=b["mint"], pool=curve, coin_vault=b["coin_vault"],
                                              quote_vault=curve, quote_mint=WSOL)
                continue
            tape = VaultTape(b["coin_vault"], b["quote_vault"], mint=b["pool"])
            vaults[b["coin_vault"]] = vaults[b["quote_vault"]] = tape
            bindings[b["pool"]] = PoolBinding(mint=b["mint"], pool=b["pool"], coin_vault=b["coin_vault"],
                                              quote_vault=b["quote_vault"], quote_mint=b["quote_mint"])
        self.cache.keep(set(bindings))
        return vaults, bindings

    # ---- the tick ----
    def tick(self, pos, binding, now):
        from sniper.services.memecoin_live_marks import fresh_quote_usd
        from sniper.services.memecoin_solana_swap_marks import CURVE_SOURCE
        from sniper.util.clock import real_utc_now_iso
        pid, mint = int(pos["id"]), str(pos.get("mint") or "")
        bound = self.binding(pid)
        if bound is None:
            return None, "binding_pending"
        if bound["binding"] == "unbound":
            return None, "unbound"
        if bound["binding"] in ("graduating", "graduation_unresolved"):
            return None, bound["binding"]
        with self._lock:
            entry = self._bindings.get(pid)
        if not entry:
            return None, "binding_pending"
        pool = stream_pool(entry)
        now_iso = (self._now_iso_fn or real_utc_now_iso)()
        quote_fn = self._quote_usd_fn or (lambda mints: fresh_quote_usd(mints, now_iso=now_iso))
        mono = self.mono()
        if self._prices[0] != int(mono):
            self._prices = (int(mono), {})
        maps = self._prices[1]
        if pool not in maps:
            maps[pool] = self.cache.fresh_price_map(pool, quote_fn(self.cache.quote_mints()),
                                                    now_ts=mono, max_age_s=5.0)
        mark = maps[pool].get(("solana", mint))
        if not mark:
            carried = self._carried(pid, mint, now, quote_fn)
            return (carried, None) if carried is not None else (None, "no_mark")
        source = str(mark.get("source"))
        if source == CURVE_SOURCE:
            at = now                       # a live curve subscription pushes every change
        else:
            seen = _epoch(mark.get("observed_at"))
            at = now - max(0.0, (_epoch(now_iso) or seen or 0.0) - (seen or 0.0))
        kind = {"fastlane_sol_swap": "sol_swap", "fastlane_sol_state": "sol_state",
                CURVE_SOURCE: "sol_curve"}.get(source, source)
        return Tick(float(mark["price"]), at, "paper_" + kind, kind,
                    _epoch(mark.get("observed_at")) or at), None

    def _carried(self, pid, mint, now, quote_fn):
        """A quiet constant-product pool's LAST SWAP, carried (Gamma 2026-10-08): the swap's own
        quote_per_token, while the pool still holds the reserves that swap left (its post-trade
        balances), the socket is live and the reserves were observed after the latest epoch
        rollover. That is funded's own swap mark extended while reserves are unchanged
        (SolanaSwapMarkCache.display_marks). The vault spot never prices it: PumpSwap trades off a
        virtual quote reserve its vault does not hold (vault/swap median 0.923, funded measured).
        Reserves that moved since the swap (an unseen trade or an LP change) carry nothing,
        counted carry_reserves_moved. None for an unknown or concentrated program, a dead socket,
        no last swap, a swap without post-trade balances or one in another quote."""
        with self._lock:
            b = self._bindings.get(int(pid))
        if not b or b.get("stale") or b.get("program") not in set(_cpmm_programs().values()):
            return None
        if not self._socket_live:
            return None
        cache, key = self.cache.get(b["pool"]), (mint, b["pool"])
        if cache is None:
            return None
        with cache._lock:
            pair, at = cache._display_pair.get(key), cache._display_pair_at.get(key)
            seen, rollover = cache._display_seen.get(key), cache._rollover_ts
            swap, post = cache._display_last_swap.get(key) or (None, None)
        if swap is None or post is None or pair is None or swap.quote_mint != b["quote_mint"]:
            return None
        if rollover is not None and (seen is None or seen < rollover):
            return None
        if pair[1] != post:
            self.counters["carry_reserves_moved"] += 1
            return None
        try:
            price = float(swap.quote_per_token) * float(
                (quote_fn(self.cache.quote_mints()) or {}).get(b["quote_mint"]))
        except (TypeError, ValueError):
            return None
        if not price > 0:
            return None
        return Tick(price, now, "paper_sol_state_carried", "sol_state_carried", _epoch(at) or now)

    # ---- threads ----
    def stream_forever(self, sleep=time.sleep):
        """The stream thread: one paper epoch at a time while anything is bound; reconnects with a
        bounded backoff. Never raises."""
        from sniper.services.memecoin_solana_swap_marks import (
            curve_seed_fn, run_price_stream_epoch, vault_seed_fn, ws_url_from_read_rpc)
        ws = ws_url_from_read_rpc(self.url)
        self._idents["stream"] = threading.get_ident()
        backoff = 1.0
        while True:
            vaults, bindings = self.book()
            if not vaults:
                self.cache.invalidate()
                sleep(2.0)
                continue

            def refresh():
                if not self._fresh:
                    return {}, {}
                fresh_vaults, fresh_bindings = self.book()
                return ({k: v for k, v in fresh_vaults.items() if k not in vaults},
                        {k: v for k, v in fresh_bindings.items() if k not in bindings})
            self.counters["epochs"] += 1
            self.counters["subscribed"] = len(vaults)
            try:
                kwargs = dict(ws_url=ws, duration_s=EPOCH_S, refresh_fn=refresh,
                              seed_fn=curve_seed_fn(self.url, rpc_call=self.rpc),
                              vault_seed_fn=vault_seed_fn(self.url, rpc_call=self.rpc))
                kwargs["stream_fn"] = self._stream_fn or self._default_stream()
                self._socket_live = True
                try:
                    asyncio.run(run_price_stream_epoch(vaults, bindings, self.cache, **kwargs))
                finally:
                    self._socket_live = False
                backoff = 1.0
            except Exception:
                self.counters["stream_errors"] += 1
                logger.exception("paper sol ticks: stream epoch failed (price rails hold)")
                sleep(backoff)
                backoff = min(backoff * 2.0, 60.0)

    @staticmethod
    def _default_stream():
        import functools

        import websockets
        from sniper.services.memecoin_solana_ws import run_vault_stream
        return functools.partial(run_vault_stream, connect=functools.partial(
            websockets.connect, ping_interval=PING_S, ping_timeout=PING_S))

    def resolve_forever(self, sleep=time.sleep, period_s=1.0):
        self._idents["bind"] = threading.get_ident()
        while True:
            try:
                self.resolve_pass()
            except Exception:
                logger.exception("paper sol ticks: resolver pass failed")
            sleep(period_s)

    def _thread_cpu(self):
        out = {}
        for name, ident in self._idents.items():
            try:
                out[name] = time.clock_gettime(time.pthread_getcpuclockid(ident))
            except (OSError, AttributeError, ValueError):
                out[name] = None           # the thread is gone or the platform cannot say: unknown
        return out

    def heartbeat(self):
        with self._lock:
            kinds = collections.Counter(b["binding"] for b in self._bindings.values())
        now, cpu, rows = time.monotonic(), self._thread_cpu(), self.counters["rows"]
        rates = {}
        if self._last_beat is not None:
            dt = now - self._last_beat[0]
            if dt > 0:
                rates["rows_per_min"] = round((rows - self._last_beat[2]) / dt * 60.0, 1)
                for name, value in cpu.items():
                    before = self._last_beat[1].get(name)
                    rates["cpu_pct_%s" % name] = (None if value is None or before is None
                                                  else round((value - before) / dt * 100.0, 2))
        self._last_beat = (now, cpu, rows)
        return dict(self.counters, bindings=dict(kinds), wanted=len(self.wanted), **rates)


def start(env):
    """The Solana source, its resolver and stream threads, or None when the key guard refuses."""
    from sniper.execution.solana_rpc_pool import key_id
    url = str(env.get(KNOB) or "").strip()
    why = key_refusal(url, env)
    if why is not None:
        logger.warning("paper sol ticks: stream NOT started (%s); Solana v1 price rails hold%s",
                       why, "" if why in ("unset", "keyless") else " (key %s)" % key_id(url))
        return None
    src = PaperSolTicks(url)
    threading.Thread(target=src.resolve_forever, name="memecoin-paper-sol-bind", daemon=True).start()
    threading.Thread(target=src.stream_forever, name="memecoin-paper-sol-stream", daemon=True).start()
    logger.info("paper sol ticks: stream started on key %s", key_id(url))
    return src

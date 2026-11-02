"""Base 8453 fast lane, step 1: the runner's lane (MEMECOIN_BASE_FAST_LANE=runner_shadow | live).

Plan docs/superpowers/plans/2026-10-14-base-8453-fast-lane-step1.md (part B; Alpha approved A-E).
The 8453 watcher decides from the source buy's own receipt (memecoin_base_fast_decide) and sends a
would-send decision of an enabled kind (MEMECOIN_BASE_FAST_ROUTES: v4_native, v4_weth) as one
datagram. This lane is the 4663 lane's executor with Base's pieces, as the BSC lane is:

- the datagram must come from the 8453 watcher's own pid (its heartbeat) and carry chain 8453;
- the wallet snapshot (native, WETH, pending nonce, gas price), the safety cache, the coin's facts
  and the inline check on a cache miss read ONLY the lane's own host (D1,
  MEMECOIN_BASE_FAST_LANE_RPC_URL), off the signal path except the bounded inline check;
- runner_shadow judges every gate of the funded hook on a rolled-back read-only connection;
- live reserves through the funded hook, signs with the chain context's account and submits the
  identical bytes through the context's shared transport (auto_free). Events are the shared lane's
  (signal_source rh_fast_lane, origin rhfast:8453:<tx>:<mint>, delivery_lane base_fast_lane), so
  the funded recovery, the ordinary lane's hold and every origin parser already apply (part A).

Default off. Alpha sets runner_shadow after the eth_call template proof on D1 passes; live needs the
operator's word and Gamma's read of the shadow.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
import time

from sniper.services.memecoin_rh_fast_lane import LaneDispatch, LiveLane, RunnerShadow

logger = logging.getLogger(__name__)

CHAIN = "8453"
CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
ROUTE_KINDS = ("v4_native", "v4_weth")
_PREFIX = "\0sniper-base-fast-lane-"
REQUIRED = ("chain", "tx_hash", "seq", "wallet", "mint", "source_user_id", "arrival_utc", "route_kind",
            "key", "pool", "cap_num", "cap_den", "clip_wei", "min_out", "post_sqrt", "src_in_wei",
            "src_out", "post_wei_num", "post_wei_den", "in_is_c0", "paid_usd", "source_fill",
            "eth_usd", "eth_usd_at", "sent_epoch", "preconf")


class _Base:
    CHAIN_ID = CHAIN_ID
    DELIVERY_LANE = "base_fast_lane"
    SAFETY_LABEL = "base_fast_safety_cache"


class BaseShadow(_Base, RunnerShadow):
    """runner_shadow on 8453: the shared shadow's checks and rolled-back hook verdict."""


class BaseLive(_Base, LiveLane):
    """live on 8453: the shared executor (lock without waiting, nonce floor, write-ahead, identical
    bytes through the context's shared transport, booking from the receipt)."""

    BACKEND = "base_fast_lane"
    RELEASE_PREFIX = "base_fast_"

    def _post(self, raw, payload):
        raise RuntimeError("base fast lane: no submission transport (the lane never has a sender)")


def lane_address(root):
    """The Base lane's abstract socket name for this state root (never the 4663 or 56 lane's)."""
    return _PREFIX + hashlib.sha256(str(root).encode()).hexdigest()[:12]


def default_address():
    from sniper.util.state_paths import state_root
    return lane_address(state_root())


def watcher_beat_path():
    from sniper.util.state_paths import state_path
    return str(state_path("data", "onchain_evm_watch.%s.heartbeat" % CHAIN))


def _hex(value, size):
    if not (isinstance(value, str) and value.startswith("0x") and len(value) == 2 + 2 * size):
        return False
    try:
        bytes.fromhex(value[2:])
        return True
    except ValueError:
        return False


def check_sender(payload, pid, uid, *, watcher_pid):
    """None when the datagram is the 8453 watcher's and well formed, else why it is dropped."""
    if uid != os.getuid():
        return "foreign_uid"
    if watcher_pid is None:
        return "no_watcher"
    if pid != watcher_pid:
        return "not_the_watcher"
    if not isinstance(payload, dict) or any(k not in payload for k in REQUIRED):
        return "malformed"
    try:
        if (payload["chain"] != CHAIN or payload["route_kind"] not in ROUTE_KINDS
                or not _hex(payload["tx_hash"], 32) or not _hex(payload["mint"], 20)
                or not _hex(payload["wallet"], 20) or not _hex(payload["pool"], 32)
                or not isinstance(payload["key"], dict) or not isinstance(payload["source_fill"], dict)
                or type(payload["in_is_c0"]) is not bool or type(payload["preconf"]) is not bool
                or type(payload["seq"]) is not int or payload["seq"] <= 0
                or not isinstance(payload["source_user_id"], str) or not payload["source_user_id"]
                or any(type(payload[k]) is not int or payload[k] <= 0 for k in (
                    "cap_num", "cap_den", "clip_wei", "min_out", "post_sqrt", "src_in_wei",
                    "post_wei_num", "post_wei_den"))
                or int(payload["src_out"]) <= 0
                or any(isinstance(payload[k], bool) or not isinstance(payload[k], (int, float))
                       or not math.isfinite(payload[k]) or payload[k] <= 0
                       for k in ("paid_usd", "eth_usd", "eth_usd_at", "sent_epoch"))):
            return "malformed"
    except (TypeError, ValueError):
        return "malformed"
    return None


def verdict_class(reason):
    """A judged signal's verdict, bounded for counting: hook:<status> for the funded hook's verdict,
    else the reason up to its first colon (built, lock_busy, float_short+built, no_snapshot...)."""
    text = str(reason or "none")
    return ":".join(text.split(":")[:2]) if text.startswith("hook:") else text.split(":")[0]


class LaneStats:
    """The lane's cumulative counters since start, for the runner heartbeat only (Alpha 2026-10-14):
    plain increments under one lock on the listener thread; the heartbeat writer copies them. A
    counter that never moved reads 0; drops and verdicts are {reason: n}, empty when none."""

    def __init__(self):
        import collections
        self._lock = threading.Lock()
        self.received, self.judged = 0, 0
        self.dropped, self.verdicts = collections.Counter(), collections.Counter()

    def receive(self):
        with self._lock:
            self.received += 1

    def drop(self, reason):
        with self._lock:
            self.dropped[str(reason)] += 1

    def verdict(self, rec):
        with self._lock:
            self.judged += 1
            self.verdicts[verdict_class((rec or {}).get("reason") if isinstance(rec, dict) else None)] += 1

    def snapshot(self):
        with self._lock:
            return {"datagrams": {"received": self.received, "dropped": dict(self.dropped)},
                    "signals_judged": self.judged, "verdicts": dict(self.verdicts)}


class BaseDispatch(LaneDispatch):
    """The shared dispatch with Base's sender (the 8453 watcher's pid) and the lane's own wallet
    snapshot attached to each signal (the 4663 detector sends its own; this lane reads it on D1)."""

    def __init__(self, *, snapshot=None, **kw):
        super().__init__(**kw)
        self.snapshot = snapshot
        self.stats = LaneStats()

    def _on_datagram(self, payload, pid, uid, recv_epoch):
        """The shared dispatch's signal path (4663's own is untouched): no candidates list and no
        shared route kinds here, so the live lane's hold is always released by this thread."""
        from sniper.services import memecoin_rh_fast_lane as rh
        from sniper.services.memecoin_bsc_fast_lane import watcher_pid
        self.stats.receive()
        why = check_sender(payload, pid, uid, watcher_pid=watcher_pid(self.beat, recv_epoch))
        if why is not None:
            self.drops.add(why)
            self.stats.drop(why)
            return None
        if not self.seen.first(payload):
            self.drops.add("duplicate")
            self.stats.drop("duplicate")
            return None
        self.counts["signals"] += 1
        payload = dict(payload, snapshot=self.snapshot.current() if self.snapshot is not None else None)
        if self.live is None:
            rec = self._on_signal(payload, recv_epoch)
            self.stats.verdict(rec)
            return rec
        rh.LANE_HOLDS.hold(payload["tx_hash"], payload["mint"])
        try:
            rec = self._on_signal(payload, recv_epoch)
            self.stats.verdict(rec)
            return rec
        finally:
            rh.LANE_HOLDS.release(payload["tx_hash"], payload["mint"])


class _NoRotation:
    """The lane's host is one URL, not a rotated research key: no rotation failures to count."""

    @staticmethod
    def stats():
        return None, []

    @staticmethod
    def provider_errors():
        return 0


def lane_clip_usd():
    """The pilot clip, bound by the funded 8453 per-entry ceiling (the step-0 decide's own clip)."""
    from sniper.services.memecoin_base_fast_decide import lane_clip_usd as clip
    return clip()


def build_base_lane(contexts, *, log, mode, root=None, connect=None, ro_connect=None, dex_base=None,
                    url=None):
    """The lane's production objects for `mode` (runner_shadow | live), or None (off, no 8453
    context, no own host, a host another role uses, or live without a shared transport). Nothing
    here starts a thread or opens a network connection."""
    from sniper.execution.evm_token_safety import build_evm_token_safety
    from sniper.execution.exec_lock import evm_exec_lock_key
    from sniper.services import memecoin_rh_fast_lane as rh
    from sniper.services.memecoin_bsc_fast_lane import LaneHost, Seen, Snapshot, lane_only_http
    from sniper.services.memecoin_evm_gas_topup import gas_pass_bounds
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now
    from sniper.util.state_paths import state_path, state_root
    if mode not in ("runner_shadow", "live"):
        return None
    ctx = (contexts or {}).get(CHAIN)
    address = (ctx or {}).get("address") if isinstance(ctx, dict) else None
    if not address or not callable((ctx or {}).get("v4_direct_quote_fn")):
        log("[base-fast-lane] %s set but no 8453 context: lane off" % mode)
        return None
    url = cfg.memecoin_base_fast_lane_rpc_url() if url is None else url
    refusal = (rh.safety_host_refusal(url, os.environ, own_name="MEMECOIN_BASE_FAST_LANE_RPC_URL")
               if url else "no MEMECOIN_BASE_FAST_LANE_RPC_URL")
    if refusal is not None:
        log("[base-fast-lane] %s set but the lane host is refused (%s): lane off" % (mode, refusal))
        return None
    if mode == "live" and ctx.get("submission_transport") is None:
        log("[base-fast-lane] live set but the 8453 context has no shared transport: lane off")
        return None
    root = state_root() if root is None else root
    db_path = str(state_path("data", "memecoin_base_fast_lane.db"))
    key = evm_exec_lock_key(CHAIN, address)
    clip = lane_clip_usd()
    paced = rh.PacedHttp(min_gap_s=cfg.MEMECOIN_BASE_FAST_SAFETY_MIN_GAP_S, pause_s=120.0)
    http = lane_only_http(paced, url)
    host = LaneHost(url, http)
    registry, times = {}, rh.SafetyTimes()

    def facts(mint, host=host):
        # Decimals and chain totalSupply, once per coin, on the lane's own host.
        decimals = int(host("eth_call", [{"to": mint, "data": "0x313ce567"}, "latest"]), 16)
        supply = int(host("eth_call", [{"to": mint, "data": "0x18160ddd"}, "latest"]), 16)
        return decimals, (supply or None)

    def safety_on(seam, *, path, **deadline):
        def quote(mint, quote_token, amount, slippage_bps=None, **_ignored):
            entry = registry.get(str(mint).lower())
            if entry is None:
                return None
            return rh.route_quote(ctx["v4_direct_quote_fn"], entry, mint, quote_token, amount,
                                  host=url, http=seam, slippage_bps=slippage_bps, chain=CHAIN)
        check = build_evm_token_safety(
            chain_id=CHAIN, rpc_url=url, taker=address, quote_fn=quote, http=seam,
            sell_slippage_bps=int(round(cfg.MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT * 100)),
            sell_route_label="base_fast_v4_direct", **deadline)
        return rh.routed_safety(check, registry, url, seam, path=path, times=times)
    # A provider failure in the background check is no verdict (never a cached refusal), as on 4663.
    safety_fn = rh.background_safety(safety_on(http, path="cache"), _NoRotation, paced)
    inline_paced = rh.PacedHttp(min_gap_s=0.0, pause_s=120.0)
    inline_http = lane_only_http(inline_paced, url)
    inline = dict(
        inline_safety_fn=safety_on(inline_http, path="inline", safety_deadline_scope=rh.inline_deadline,
                                   safety_deadline_s=cfg.MEMECOIN_BASE_FAST_INLINE_SAFETY_DEADLINE_S),
        inline_facts_fn=lambda mint: facts(mint, host=LaneHost(url, inline_http)),
        inline_deadline_s=cfg.MEMECOIN_BASE_FAST_INLINE_SAFETY_DEADLINE_S, inline_host=inline_paced)
    dex = rh.DexBudget(dex_base)

    def dex_stats(mint):
        return dex.stats(mint, network_id=CHAIN)
    cache = rh.SafetyCache(safety_fn, ttl_s=cfg.MEMECOIN_BASE_FAST_SAFETY_TTL_S, usd=clip,
                           facts_fn=facts, stats_fn=dex_stats,
                           refresh_lead_s=cfg.MEMECOIN_RH_FAST_SAFETY_REFRESH_LEAD_S,
                           supply_ttl_s=cfg.MEMECOIN_FAST_LANE_SUPPLY_TTL_S,
                           facts_sink=ctx.get("put_token_facts_fn"))
    snapshot = Snapshot(host, address, weth=WETH, lane="base")
    # The source receipt's post-state is exact by construction (the 4663 rule asks a tape's).
    common = dict(clip_usd=clip, low_wei=int(gas_pass_bounds(CHAIN, cfg)["low_water_wei"]),
                  generation_fn=rh.lane_generation_fn(key, CHAIN),
                  max_snapshot_age_s=cfg.MEMECOIN_BASE_FAST_LANE_SNAPSHOT_MAX_AGE_S,
                  eth_usd_max_age_s=cfg.MEMECOIN_BASE_FAST_LANE_ETH_USD_MAX_AGE_S,
                  generation_margin_s=cfg.MEMECOIN_BASE_FAST_LANE_GENERATION_MARGIN_S,
                  facts_fn=cache.facts, ctx=ctx, contexts=contexts, require_exact_state=False)
    if ro_connect is None:
        from sniper.persistence.db_core import get_readonly_connection
        ro_connect = get_readonly_connection
    shadow = BaseShadow(lock_key=key, lock_dir=None, taker=address, now_fn=real_utc_now,
                        ro_connect=ro_connect, fresh_stats_fn=dex_stats,
                        check_sink=rh.stats_check_sink(db_path), **common)
    live = None
    if mode == "live":
        from sniper.execution.evm_wallet import _rpc
        from sniper.persistence.db_core import get_connection
        conn = (connect or get_connection)()
        conn.execute("PRAGMA busy_timeout=200")
        fills, sends = rh._seed_pilot(conn, network_id=CHAIN)
        rpc_url = ctx.get("rpc_url")

        def rpc_read(method, params):
            return (_rpc(rpc_url, method, params) or {}).get("result")

        def balance_at(mint, block):
            data = "0x70a08231" + str(address)[2:].lower().rjust(64, "0")
            for _ in range(10):                 # the custody node can trail the head by a few blocks
                result = rpc_read("eth_call", [{"to": mint, "data": data}, hex(int(block))])
                if isinstance(result, str) and len(result) > 2:
                    return int(result, 16)
                time.sleep(0.5)
            return None
        live = BaseLive(
            conn=conn, sender=None, fanout_fn=lambda raw: None, rpc_read=rpc_read, lock_key=key,
            lock_dir=None, host_clock=time.time, pilot_fills=cfg.MEMECOIN_BASE_FAST_LANE_PILOT_FILLS,
            max_sends_per_hour=cfg.MEMECOIN_BASE_FAST_LANE_MAX_SENDS_PER_HOUR, fills=fills,
            send_times=sends, balance_at_fn=balance_at,
            lock_receipt_cap_s=cfg.MEMECOIN_BASE_FAST_LANE_LOCK_RECEIPT_CAP_S,
            check_sink=rh.fill_check_sink(db_path), cache=cache, routes=cfg.MEMECOIN_BASE_FAST_ROUTES,
            stats_fn=dex_stats, **inline, **common,
            revert_reason_fn=lambda h: rh._revert_reason(h, rpc_url))
        live.shared_connect = connect or get_connection
        log("[base-fast-lane] live route kinds: %s" % (",".join(sorted(live.routes)) or "none"))
    dispatch = BaseDispatch(snapshot=snapshot, shadow=shadow, cache=cache, registry=registry,
                            seen=Seen(), drops=rh.Drops(), db_path=db_path, beat=watcher_beat_path(),
                            live=live)
    return {"dispatch": dispatch, "cache": cache, "safety_fn": safety_fn, "paced": paced,
            "inline_paced": inline_paced, "dex": dex, "snapshot": snapshot, "live": live,
            "address_name": lane_address(root), "db_path": db_path, "url": url, "times": times}


_LANE_RUNS = []             # (stop event, threads), one per start_base_lane
# The running lane, for the runner heartbeat's base_fast_lane dict: set once by start_base_lane on
# the runner's main thread, cleared by stop_base_lane; read only by status() (the heartbeat writer).
_ACTIVE = {}


def status(now=None):
    """The runner heartbeat's base_fast_lane dict, or None while no lane runs in this process (off,
    step0, refused at start: the heartbeat then has no key). Unknown is None, never 0: the
    snapshot's last success age before its first good read."""
    lane, mode = _ACTIVE.get("lane"), _ACTIVE.get("mode")
    if lane is None:
        return None
    now = time.time() if now is None else now
    snap = lane["snapshot"]
    last = snap.current()
    paced = {name: {"calls": lane[name].counts.get("calls", 0),
                    "http_429": lane[name].counts.get("http_429", 0),
                    "transport_errors": lane[name].counts.get("transport_errors", 0)}
             for name in ("paced", "inline_paced") if lane.get(name) is not None}
    return {"mode": mode,
            "snapshot": {"reads": snap.counts.get("reads", 0), "errors": snap.counts.get("errors", 0),
                         "last_success_age_s": (now - float(last["read_at"])) if last else None},
            **paced, **lane["dispatch"].stats.snapshot()}


def stop_base_lane(timeout_s=5.0):
    """Stop every thread start_base_lane started in this process (tests, an orderly shutdown);
    returns the names of threads still alive after `timeout_s`."""
    runs, _LANE_RUNS[:] = list(_LANE_RUNS), []
    _ACTIVE.clear()
    for stop, _threads in runs:
        stop.set()
    deadline = time.monotonic() + float(timeout_s)
    alive = []
    for _stop, threads in runs:
        for thread in threads:
            if thread.ident is None:
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                alive.append(thread.name)
    return alive


def start_base_lane(contexts, *, log, root=None):
    """Start the Base lane when MEMECOIN_BASE_FAST_LANE is runner_shadow or live, an 8453 context
    exists and the lane's own host is set and free: the datagram listener, the wallet snapshot and
    the safety cache (both on that host), and, live, the operator's stop file. Returns the listener
    thread, or None. Never raises into the runner."""
    from sniper.services import memecoin_rh_fast_lane as rh
    from sniper.settings import memecoin as cfg
    try:
        mode = cfg.memecoin_base_fast_lane_mode()
        lane = build_base_lane(contexts, log=log, mode=mode, root=root,
                               dex_base=rh.lane_dex_base(None))
        if lane is None:
            return None
    except Exception:
        logger.exception("base fast lane: start failed (lane off)")
        return None
    stop, threads = threading.Event(), []
    _LANE_RUNS.append((stop, threads))
    _ACTIVE.update(lane=lane, mode=mode)
    dispatch, cache, live = lane["dispatch"], lane["cache"], lane["live"]
    if live is not None:
        rh.apply_stop_file(live, rh.stop_file_path(), log)

    def run():
        rh.warm_hook(dispatch.shadow, log=log)
        sock, bind_state, flushed = None, {}, time.monotonic()
        while not stop.is_set():
            try:
                if time.monotonic() - flushed >= 60:
                    flushed = time.monotonic()
                    dispatch.drops.flush(lane["db_path"])
                    lane["times"].flush(lane["db_path"])
                if sock is None:
                    sock = rh.bind_or_log(lane["address_name"], bind_state, log)
                    if sock is None:
                        stop.wait(1.0)
                        continue
                got = sock.recv(1.0)
                if got is None:
                    continue
                payload, pid, uid = got
                dispatch.on_datagram(payload, pid, uid, time.time())
            except Exception:
                logger.exception("base fast lane: signal handling failed (lane keeps running)")
                stop.wait(0.1)
        if sock is not None:
            sock.close()

    def snapshot_loop():
        while not stop.is_set():
            lane["snapshot"].poll_once()                # never raises; a failure ages out
            stop.wait(cfg.MEMECOIN_BASE_FAST_LANE_SNAPSHOT_PERIOD_S)

    def safety_loop():
        while not stop.is_set():
            try:
                mint = cache.next_due(time.time())
                if mint is None:
                    stop.wait(1.0)
                    continue
                cache.run_one(mint, now=time.time())
            except Exception:
                logger.exception("base fast safety: verdict loop failed (keeps running)")
                stop.wait(1.0)

    def stop_watch():
        path = rh.stop_file_path()
        while not stop.is_set():
            try:
                rh.apply_stop_file(live, path, log)
            except Exception:
                logger.exception("base fast lane: stop-file check failed")
            stop.wait(1.0)

    threads += [threading.Thread(target=run, name="base-fast-lane", daemon=True),
                threading.Thread(target=snapshot_loop, name="base-fast-snapshot", daemon=True),
                threading.Thread(target=safety_loop, name="base-fast-safety", daemon=True)]
    if live is not None:
        threads.append(threading.Thread(target=stop_watch, name="base-fast-stop", daemon=True))
    for thread in threads:
        thread.start()
    log("[base-fast-lane] %s started%s; snapshot and safety on the lane's own host" % (
        mode, ": LIVE, $%.0f pilot clip, routes %s" % (lane_clip_usd(), ",".join(sorted(live.routes)) or "none")
        if live is not None else ": no signing, no reservation, no send"))
    return threads[0]

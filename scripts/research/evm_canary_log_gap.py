#!/usr/bin/env python3
"""EVM canary log-gap recorder: D2 step 1 (spec 2026-09-30-memecoin-j7-shadow-fallback-design.md).

WHY. The EVM watcher's transfer filters are sparse by design, so a socket whose heads flow while its
filters deliver nothing is invisible when no watched wallet trades on J7 (the 09-29 05:30Z failure
shape on a quiet chain). The proposed fix is a canary filter on a busy token's Transfer logs. It
only works if the canary's natural silences are short and rare. This measures them over 24 h.

WHAT. One process, one research WebSocket, `newHeads` plus `logs` for one token's Transfer topic.
Gaps are measured between the first notification of consecutive canary blocks on one connection.
Silences of 10 s or more are checked with eth_getLogs on the same research host: logs found in the
silent block range are a dead filter; none found is natural quiet. The rules are fixed in
docs/audits/2026-09-30-evm-canary-log-gap-preregistration.md.

NEVER a watcher, funded or capture host: `--ws-url` must be a research host no role uses (taken
domains are refused). Writes one JSON file, no database.

    python3 scripts/research/evm_canary_log_gap.py --chain 8453 \
        --ws-url wss://base.gateway.tenderly.co --http-url https://base.gateway.tenderly.co \
        --token 0x4200000000000000000000000000000000000006 --duration 86400 --out <file>.json
    python3 scripts/research/evm_canary_log_gap.py --report <file>.json
"""
import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
LONG_S = 10.0          # a gap this long is listed and checked with eth_getLogs
DEAD_MIN_S = 30.0      # a verified silence this long, heads flowing, is a dead-filter episode
FORCE_S = 120.0        # research socket only: no canary log this long while heads flow -> reconnect
FORCE_MIN_HEADS = 3
SILENT_S = 60.0        # nothing at all on the socket this long -> reconnect
VERIFY_MIN_PERIOD_S = 5.0
POLL_PERIOD_S = 60.0   # addendum 2: chain-side polling
POLL_LAG_BLOCKS = 5
POLL_CHUNK_BLOCKS = 200
POLL_MAX_FAILS = 3
EXACT_MIN_GAP_S = 2.0  # addendum 3 correction: a clamped gap this long reads both ends' milliTimestamp
NODEREAL_CU = {"eth_blockNumber": 5, "eth_getBlockByNumber": 15, "eth_getLogs": 50, "eth_chainId": 5}


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


class GapBook:
    """Gap accounting, pure: every method takes the clock. One connection at a time."""

    def __init__(self):
        self.hist = Counter()          # gap rounded to 0.01 s -> count
        self.hour_max = {}             # UTC hour -> longest gap ending in it
        self.hour_hist = {}            # UTC hour -> {gap: count}, so quiet hours show
        self.long = []                 # gaps >= LONG_S, plus forced silences
        self.censored = []
        self.connects = 0
        self.connected_s = 0.0
        self.logs = self.heads = self.removed = 0
        self.frames = self.bytes = 0
        self.hour_frames = Counter()   # UTC hour -> notifications (the provider's billing unit)
        self.conn_at = None
        self._reset_conn()

    def _reset_conn(self):
        self.last_block = self.last_at = self.last_wall = None
        self.heads_in_gap, self.max_head_gap, self.prev_head_at = 0, 0.0, None
        self.newest_head = None

    def on_frame(self, wall, nbytes):
        self.frames += 1
        self.bytes += nbytes
        self.hour_frames[_utc(wall)[:13]] += 1

    def on_connect(self, now):
        self.connects += 1
        self.conn_at = now
        self._reset_conn()

    def on_head(self, now, number):
        self.heads += 1
        self.heads_in_gap += 1
        if self.prev_head_at is not None:
            self.max_head_gap = max(self.max_head_gap, now - self.prev_head_at)
        self.prev_head_at = now
        self.newest_head = number if self.newest_head is None else max(self.newest_head, number)

    def on_log(self, now, wall, block, removed=False):
        """Returns a long-gap record to verify, or None."""
        self.logs += 1
        if removed:
            self.removed += 1
        if self.last_block is not None and block <= self.last_block:
            return None                               # same block, or a reorg: one delivery
        rec = None
        if self.last_at is not None:
            gap = now - self.last_at
            self.hist["%.2f" % gap] += 1
            hour = _utc(wall)[:13]
            self.hour_max[hour] = max(self.hour_max.get(hour, 0.0), round(gap, 3))
            self.hour_hist.setdefault(hour, Counter())["%.2f" % gap] += 1
            if gap >= LONG_S:
                rec = {"kind": "gap", "gap_s": round(gap, 3), "start": _utc(self.last_wall),
                       "end": _utc(wall), "from_block": self.last_block, "to_block": block,
                       "heads": self.heads_in_gap, "max_head_gap_s": round(self.max_head_gap, 3),
                       "verify": None}
                self.long.append(rec)
        self.last_block, self.last_at, self.last_wall = block, now, wall
        self.heads_in_gap, self.max_head_gap = 0, 0.0
        return rec

    def should_force(self, now):
        return (self.last_at is not None and now - self.last_at >= FORCE_S
                and self.heads_in_gap >= FORCE_MIN_HEADS)

    def on_disconnect(self, now, wall, reason):
        """Close the connection. An open silence is censored; a forced one is returned to verify."""
        if self.conn_at is not None:
            self.connected_s += now - self.conn_at
        self.conn_at = None
        rec = None
        if self.last_at is not None:
            rec = {"kind": reason, "silence_s": round(now - self.last_at, 3), "start": _utc(self.last_wall),
                   "end": _utc(wall), "from_block": self.last_block, "to_block": self.newest_head,
                   "heads": self.heads_in_gap, "max_head_gap_s": round(self.max_head_gap, 3),
                   "verify": None}
            self.censored.append(rec)
        self._reset_conn()
        return rec if reason == "forced" else None

    def state(self, now):
        live = (now - self.conn_at) if self.conn_at is not None else 0.0
        return {"connects": self.connects, "connected_s": round(self.connected_s + live, 1),
                "logs": self.logs, "heads": self.heads, "removed": self.removed,
                "frames": self.frames, "bytes": self.bytes, "hour_frames": dict(self.hour_frames),
                "gap_hist": dict(self.hist), "hour_max_gap_s": self.hour_max,
                "hour_hist": {h: dict(c) for h, c in self.hour_hist.items()},
                "long": self.long, "censored": self.censored}


class ChainPoll:
    """Chain-side canary gaps from eth_getLogs reads (pre-registration addendum 2).

    Every block between two canary blocks was read and held no canary log, so every gap here is
    natural by construction. A block's time is the window mean between consecutive `hi` header
    `milliTimestamp`s, clamped into its log's 1 s `blockTimestamp` bucket: the mean alone erases a
    block-production stall (Gamma MEDIUM, addendum 3). A chunk that fails POLL_MAX_FAILS cycles in a
    row is unread: the gap across it is censored."""

    def __init__(self, book, token):
        self.book, self.token = book, token
        self.anchor = None           # (block, ts) of the previous hi
        self.next_block = None       # first block not yet read
        self.fails = 0
        self.unread = []
        self.window_max_s, self.windows_over, self.clamped, self.no_block_ts = 0.0, 0, 0, 0
        self.exact = self.exact_failed = 0

    @staticmethod
    def _block_time(head):
        return (int(head["milliTimestamp"], 16) / 1000.0 if head.get("milliTimestamp")
                else float(int(head["timestamp"], 16)))

    def chain_now(self):
        return self.anchor[1] if self.anchor else 0.0

    def cycle(self, rpc, sleep=time.sleep):
        hi = int(rpc("eth_blockNumber", []), 16) - POLL_LAG_BLOCKS
        ts_hi = self._block_time(rpc("eth_getBlockByNumber", [hex(hi), False]))
        if self.anchor is None:
            self.anchor, self.next_block = (hi, ts_hi), hi + 1
            self.book.on_connect(ts_hi)
            return
        a_block, a_ts = self.anchor
        if hi <= a_block:
            return
        block_s = (ts_hi - a_ts) / (hi - a_block)
        self.window_max_s = max(self.window_max_s, block_s)
        self.windows_over += block_s > 0.6
        at = lambda b: a_ts + (b - a_block) * block_s  # noqa: E731
        while self.next_block <= hi:
            lo, end = self.next_block, min(hi, self.next_block + POLL_CHUNK_BLOCKS - 1)
            sleep(1.0)                                  # at most one getLogs a second (150 CU/s cap)
            try:
                logs = rpc("eth_getLogs", [{"address": self.token, "topics": [TRANSFER],
                                             "fromBlock": hex(lo), "toBlock": hex(end)}])
            except Exception:
                self.fails += 1
                if self.fails < POLL_MAX_FAILS:
                    return                              # retried next cycle from the same block
                self.unread.append([lo, end])
                self.book.on_disconnect(at(lo), at(lo), "unread")
                self.book.on_connect(at(end))
                self.fails, self.next_block = 0, end + 1
                continue
            self.fails = 0
            stamp = {}
            for x in logs:
                if x.get("blockTimestamp"):
                    stamp[int(x["blockNumber"], 16)] = int(x["blockTimestamp"], 16)
            for b in sorted({int(x["blockNumber"], 16) for x in logs}):
                t = at(b)
                if b not in stamp:
                    self.no_block_ts += 1
                elif not stamp[b] <= t < stamp[b] + 1:
                    t = min(max(t, stamp[b]), stamp[b] + 0.999)
                    self.clamped += 1
                prev = self.book.last_block
                if prev is not None and t - self.book.last_at >= EXACT_MIN_GAP_S:
                    try:                                # each clamped end can be 1 s off: read both ends
                        t_prev = self._block_time(rpc("eth_getBlockByNumber", [hex(prev), False]))
                        t = self._block_time(rpc("eth_getBlockByNumber", [hex(b), False]))
                        self.book.last_at = self.book.last_wall = t_prev   # the open gap starts at the exact time
                        self.exact += 1
                    except Exception:
                        self.exact_failed += 1
                rec = self.book.on_log(t, t, b)
                if rec is not None:
                    rec["verify"] = 0                   # the read itself: no canary log in between
            self.next_block = end + 1
        self.anchor = (hi, ts_hi)


def quantile(hist, q):
    """The q-quantile of a {'1.23': count} histogram, or None when empty."""
    items = sorted((float(k), n) for k, n in hist.items())
    total = sum(n for _, n in items)
    if not total:
        return None
    need, seen = q * total, 0
    for value, n in items:
        seen += n
        if seen >= need:
            return value
    return items[-1][0]


def classify(rec, poll=False):
    """Natural, dead-filter or unknown for one checked silence (pre-registered rules). Addendum 4: on a
    socket, a gap between adjacent blocks is a delivery_stall: no block lies between them, so the empty
    getLogs range proves no quiet (heads stall with it). Chain-side polling keeps it natural (a block stall)."""
    silence = rec.get("gap_s", rec.get("silence_s")) or 0.0
    a, b = rec.get("from_block"), rec.get("to_block")
    if not poll and rec.get("kind") == "gap" and a is not None and b is not None and b - a == 1:
        return "delivery_stall"
    v = rec.get("verify")
    if not isinstance(v, int):
        return "unknown"
    if v == 0:
        return "natural"
    return "dead" if silence >= DEAD_MIN_S and rec.get("heads", 0) >= FORCE_MIN_HEADS else "missed_short"


def verdict(state, *, span_h=24.0):
    hist = state.get("gap_hist") or {}
    p99, p999 = quantile(hist, 0.99), quantile(hist, 0.999)
    out = {"gaps": sum(hist.values()), "p50": quantile(hist, 0.5), "p99": p99, "p999": p999,
           "max": max((float(k) for k in hist), default=None),
           "connected_h": round(state.get("connected_s", 0.0) / 3600.0, 2)}
    if out["connected_h"] > 0 and state.get("frames") is not None:
        per_day = 24.0 / out["connected_h"]
        out["frames_per_day"] = int(state["frames"] * per_day)
        out["mb_per_day"] = round(state.get("bytes", 0) * per_day / 1e6, 1)
        if state.get("calls"):
            out["cu_per_day"] = int(sum(NODEREAL_CU.get(m, 0) * n for m, n in state["calls"].items()) * per_day)
    out["hour_p99"] = {h: quantile(c, 0.99) for h, c in sorted((state.get("hour_hist") or {}).items())}
    checked = list(state.get("long", [])) + [c for c in state.get("censored", []) if c.get("kind") == "forced"]
    poll = state.get("mode") == "poll"
    kinds = Counter(classify(r, poll) for r in checked)
    out["checked"] = dict(kinds)
    out["dead_episodes"] = [r["start"] for r in checked if classify(r, poll) == "dead"]
    out["delivery_stalls"] = [[r.get("start"), r.get("gap_s")] for r in checked if classify(r, poll) == "delivery_stall"]
    if p999 is None:
        out["verdict"] = "insufficient"
        return out
    t = max(DEAD_MIN_S, 3.0 * p999)
    natural_long = [r for r in checked if classify(r, poll) == "natural"
                    and (r.get("gap_s", r.get("silence_s")) or 0.0) >= t]
    out["T_s"] = round(t, 2)
    out["natural_at_or_over_T"] = len(natural_long)
    if out["connected_h"] < 20.0 * span_h / 24.0:
        out["verdict"] = "insufficient"
    elif p99 <= 5.0 and len(natural_long) <= 1:
        out["verdict"] = "qualifies"
    else:
        out["verdict"] = "does_not_qualify"
    return out


def load_env():
    from sniper.util.envfiles import load_env_files
    state_root = os.getenv("SNIPER_STATE_ROOT") or str(ROOT)
    if ".env" not in load_env_files(state_root):
        raise SystemExit("refused: no .env under %s, so the taken hosts are unknown" % state_root)


def http_from_ws(url):
    """The HTTP twin of a WS endpoint: same host and key, `https`, a leading `/ws` segment dropped
    (NodeReal serves `/ws/v1/<key>` and `/v1/<key>`; tenderly, Alchemy and QuickNode share one path)."""
    parts = urlsplit(url)
    path = parts.path[3:] if parts.path.startswith("/ws/") else parts.path
    return parts._replace(scheme="https" if parts.scheme == "wss" else "http", path=path).geturl()


def refuse_taken(urls, own_vars=()):
    """No role's host may carry research load (taken_budgets, derived from the runner's env). The
    research lane's own env vars are the only ones left out of the taken set."""
    from sniper.services.memecoin_evm_exact_capture import budget, taken_budgets
    env = {k: v for k, v in os.environ.items() if k not in set(own_vars)}
    taken = taken_budgets(env)
    for url in urls:
        if budget(url) in taken:
            raise SystemExit("refused: %s is used by another role" % budget(url))
        # Also refuse a host that a capture read URL names (capture's own budget).
        for k, v in env.items():
            if k.startswith("MEMECOIN_CAPTURE_EVM_READ_URL_") and budget(v) == budget(url):
                raise SystemExit("refused: %s is a capture read host (%s)" % (budget(url), k))


def get_logs_count(http_url, token, lo, hi):
    return len(_rpc(http_url, "eth_getLogs", [{"address": token, "topics": [TRANSFER],
                                                "fromBlock": hex(lo), "toBlock": hex(hi)}]))


def _rpc(http_url, method, params, timeout=20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(http_url, data=body, headers={"Content-Type": "application/json",
                                                               "User-Agent": "sniper-research/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        reply = json.loads(r.read())
    if "error" in reply:
        raise RuntimeError(str(reply["error"])[:160])
    return reply["result"]


def block_stats(blocks_with_logs, lo, hi):
    """Coverage and the longest empty run over blocks lo..hi, from the set of blocks with a log."""
    n, run, longest = hi - lo + 1, 0, 0
    for b in range(lo, hi + 1):
        run = 0 if b in blocks_with_logs else run + 1
        longest = max(longest, run)
    return {"coverage": round(len(blocks_with_logs & set(range(lo, hi + 1))) / n, 3), "longest_empty_run": longest}


def token_rate(http_url, token, blocks):
    """Transfer logs per second and per-block coverage over the newest `blocks` blocks."""
    hi = int(_rpc(http_url, "eth_blockNumber", []), 16) - 2
    lo = hi - blocks + 1
    logs = _rpc(http_url, "eth_getLogs", [{"address": token, "topics": [TRANSFER],
                                            "fromBlock": hex(lo), "toBlock": hex(hi)}])
    t = [int(_rpc(http_url, "eth_getBlockByNumber", [hex(b), False])["timestamp"], 16) for b in (lo, hi)]
    span = max(1, t[1] - t[0])
    return dict(block_stats({int(x["blockNumber"], 16) for x in logs}, lo, hi), blocks=blocks,
                span_s=span, logs=len(logs), logs_per_s=round(len(logs) / span, 2))


async def _verify(rec, http_url, token, last_verify, lock):
    lo, hi = (rec.get("from_block") or 0) + 1, (rec.get("to_block") or 0) - (1 if rec["kind"] == "gap" else 0)
    if rec.get("from_block") is None or rec.get("to_block") is None:
        rec["verify"] = "unknown:no_range"
        return
    if hi < lo:
        rec["verify"] = 0                       # adjacent blocks: nothing in between to miss
        return
    async with lock:                            # one getLogs at a time, VERIFY_MIN_PERIOD_S apart
        wait = VERIFY_MIN_PERIOD_S - (time.monotonic() - last_verify[0])
        if wait > 0:
            await asyncio.sleep(wait)
        last_verify[0] = time.monotonic()
        try:
            rec["verify"] = await asyncio.to_thread(get_logs_count, http_url, token, lo, hi)
        except Exception as exc:
            rec["verify"] = "unknown:%s" % type(exc).__name__


def _dump(path, meta, book, now=None, **extra):
    data = dict(meta, now=_utc(time.time()), **book.state(time.monotonic() if now is None else now), **extra)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


async def run(args):
    import websockets
    token = args.token.lower()
    meta = {"chain": args.chain, "token": token, "host": urlsplit(args.ws_url).hostname,
            "started": _utc(time.time()), "duration_s": args.duration}
    book, tasks, last_verify, lock = GapBook(), set(), [0.0], asyncio.Lock()
    end = time.monotonic() + args.duration
    last_dump, backoff = 0.0, 1.0

    def spawn(rec):
        if rec is not None:
            t = asyncio.ensure_future(_verify(rec, args.http_url, token, last_verify, lock))
            tasks.add(t)
            t.add_done_callback(tasks.discard)

    while time.monotonic() < end:
        reason = "closed"
        try:
            async with websockets.connect(args.ws_url, open_timeout=15, ping_interval=20, ping_timeout=20,
                                          max_size=2 ** 24) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}))
                got = int(json.loads(await asyncio.wait_for(ws.recv(), 15))["result"], 16)
                if got != int(args.chain):
                    raise SystemExit("host answered chain %s, not %s" % (got, args.chain))
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_subscribe",
                                          "params": ["newHeads"]}))
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "eth_subscribe",
                                          "params": ["logs", {"address": token, "topics": [TRANSFER]}]}))
                book.on_connect(time.monotonic())
                backoff = 1.0
                while True:
                    now = time.monotonic()
                    if now >= end:
                        reason = "end"
                        break
                    if book.should_force(now):
                        reason = "forced"
                        break
                    if now - last_dump >= 60.0:
                        _dump(args.out, meta, book)
                        last_dump = now
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(SILENT_S, max(0.1, end - now)))
                    except asyncio.TimeoutError:
                        if time.monotonic() < end:
                            reason = "silent"
                            break
                        continue
                    book.on_frame(time.time(), len(raw))
                    msg = json.loads(raw)
                    if msg.get("id") in (2, 3) and "error" in msg:
                        raise RuntimeError("subscribe refused: %s" % str(msg["error"])[:160])
                    res = (msg.get("params") or {}).get("result")
                    if not isinstance(res, dict):
                        continue
                    now, wall = time.monotonic(), time.time()
                    if "address" in res:
                        spawn(book.on_log(now, wall, int(res["blockNumber"], 16), bool(res.get("removed"))))
                    elif "number" in res:
                        book.on_head(now, int(res["number"], 16))
        except SystemExit:
            raise
        except Exception as exc:
            reason = "error:%s" % type(exc).__name__
        spawn(book.on_disconnect(time.monotonic(), time.time(), reason))
        print("%s %s connects=%d logs=%d" % (_utc(time.time()), reason, book.connects, book.logs), flush=True)
        if reason not in ("end", "forced") and time.monotonic() < end:
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)
    if tasks:
        await asyncio.wait(tasks, timeout=60)
    _dump(args.out, meta, book)


def run_poll(args):
    token = args.token.lower()
    meta = {"chain": args.chain, "token": token, "host": urlsplit(args.http_url).hostname, "mode": "poll",
            "started": _utc(time.time()), "duration_s": args.duration}
    book = GapBook()
    poll = ChainPoll(book, token)
    calls, errors = Counter(), Counter()

    def rpc(method, params):
        calls[method] += 1
        return _rpc(args.http_url, method, params)

    end = time.monotonic() + args.duration
    while time.monotonic() < end:
        t0 = time.monotonic()
        try:
            poll.cycle(rpc)
        except Exception as exc:                        # a head or timestamp read failed: skip the cycle
            errors[type(exc).__name__] += 1
        _dump(args.out, meta, book, now=poll.chain_now(), calls=dict(calls), errors=dict(errors),
              unread=poll.unread, next_block=poll.next_block, window_max_block_s=round(poll.window_max_s, 4),
              windows_over_0_6s=poll.windows_over, clamped=poll.clamped, no_block_ts=poll.no_block_ts,
              exact=poll.exact, exact_failed=poll.exact_failed)
        time.sleep(max(0.0, min(POLL_PERIOD_S - (time.monotonic() - t0), end - time.monotonic())))
    print("%s end calls=%s errors=%s" % (_utc(time.time()), dict(calls), dict(errors)), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain")
    ap.add_argument("--ws-url")
    ap.add_argument("--http-url")
    ap.add_argument("--ws-url-env", help="read the WS URL from this env var (keyed URLs: never argv)")
    ap.add_argument("--http-url-env", help="default: the HTTP twin of the WS URL")
    ap.add_argument("--rates", help="comma-separated tokens: Transfer logs/s and block coverage, then exit")
    ap.add_argument("--blocks", type=int, default=100)
    ap.add_argument("--poll", action="store_true", help="chain-side eth_getLogs polling (addendum 2)")
    ap.add_argument("--token")
    ap.add_argument("--duration", type=float, default=86400.0)
    ap.add_argument("--out")
    ap.add_argument("--report")
    args = ap.parse_args()
    if args.report:
        state = json.loads(Path(args.report).read_text())
        print(json.dumps(dict(verdict(state), started=state.get("started"), now=state.get("now"),
                              connects=state.get("connects"), censored=len(state.get("censored", [])),
                              hour_max_gap_s=state.get("hour_max_gap_s"), calls=state.get("calls"),
                              errors=state.get("errors"), unread=state.get("unread"),
                              window_max_block_s=state.get("window_max_block_s"),
                              windows_over_0_6s=state.get("windows_over_0_6s"), clamped=state.get("clamped"),
                              no_block_ts=state.get("no_block_ts"), exact=state.get("exact"),
                              exact_failed=state.get("exact_failed")), indent=1))
        return
    load_env()
    own = [v for v in (args.ws_url_env, args.http_url_env) if v]
    if args.ws_url_env:
        args.ws_url = os.environ.get(args.ws_url_env) or None
        if not args.ws_url:
            raise SystemExit("refused: %s is not set" % args.ws_url_env)
    if args.http_url_env:
        args.http_url = os.environ.get(args.http_url_env) or None
    args.http_url = args.http_url or http_from_ws(args.ws_url)
    refuse_taken([args.ws_url, args.http_url], own_vars=own)
    if args.rates:
        for token in args.rates.split(","):
            print(json.dumps(dict(token=token, **token_rate(args.http_url, token, args.blocks))), flush=True)
        return
    if args.poll:
        run_poll(args)
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

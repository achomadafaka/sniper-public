"""Volume probe for the rabbit pre-execution pool tape (task 5a7e1967, Alpha decision b, 2026-10-17).

Before any tape build: how heavy are the two processed Yellowstone subscriptions the tape would need?
  curve: accounts owned by the pump program (every bonding-curve write, with slot and write_version)
  amm:   successful transactions that include the PumpSwap program (vault postTokenBalances, tx index)
One connection, PROCESSED, for --duration seconds (default 900). Per filter it reports frames, frames/s,
KB/s (protobuf ByteSize), the CPU of the decode the tape would do (process time per frame) and the
slots covered; for the whole run the process CPU and the stream's own slot frames.

NOT ON THE FUNDED RUNNER: a separate process. It holds ONE Yellowstone connection on the Shyft token,
which the funded runner shares (Build plan: 10 connections). It refuses to connect without
--connect: run that only once Alpha confirms a free connection slot on the Shyft dashboard.
Without --connect it prints the plan (filters, endpoint host, duration) and exits 0.

  python3 scripts/research/rabbit_tape_probe.py                 # plan only, no connection
  python3 scripts/research/rabbit_tape_probe.py --connect       # 15 min probe, JSON on stdout
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import resource
import signal
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
DEFAULT_URL = "https://grpc.fra.shyft.to"
CURVE_DISCRIMINATOR = hashlib.sha256(b"account:BondingCurve").digest()[:8]   # = memecoin_solana_ws's


def subscription():
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    return pb.SubscribeRequest(
        accounts={"curve": pb.SubscribeRequestFilterAccounts(owner=[PUMP_PROGRAM])},
        transactions={"amm": pb.SubscribeRequestFilterTransactions(
            vote=False, failed=False, account_include=[PUMP_AMM_PROGRAM])},
        slots={"head": pb.SubscribeRequestFilterSlots(filter_by_commitment=True)},
        commitment=pb.CommitmentLevel.PROCESSED)


def decode_curve(update):
    """What the tape would keep from a curve write: (pubkey, slot, write_version, v_tok, v_sol, r_tok,
    r_sol, complete), or None for a non-curve pump account (global, fee config...: Gamma LOW-1)."""
    acct = update.account.account
    raw = bytes(acct.data)
    if len(raw) < 82 or raw[:8] != CURVE_DISCRIMINATOR:
        return None
    v_tok, v_sol, r_tok, r_sol, _supply = struct.unpack_from("<5Q", raw, 8)
    return bytes(acct.pubkey), update.account.slot, acct.write_version, v_tok, v_sol, r_tok, r_sol, raw[48]


def decode_amm(update):
    """What the tape would keep from a PumpSwap tx: (slot, index, [(account index, mint, amount)])."""
    tx = update.transaction
    rows = [(b.account_index, b.mint, b.ui_token_amount.amount) for b in tx.transaction.meta.post_token_balances]
    return tx.slot, tx.transaction.index, rows


class Meter:
    def __init__(self):
        self.frames = self.bytes = self.decoded = self.skipped = self.errors = 0
        self.cpu_s = 0.0
        self.slots = set()

    def add(self, update, decode, slot):
        self.frames += 1
        self.bytes += update.ByteSize()
        t0 = time.process_time()
        try:
            out = decode(update)
        except Exception:
            self.errors += 1
            out = None
        self.cpu_s += time.process_time() - t0
        if out is None:
            self.skipped += 1
        else:
            self.decoded += 1
        self.slots.add(slot)

    def report(self, wall_s):
        return dict(frames=self.frames, frames_per_s=round(self.frames / wall_s, 1),
                    kb_per_s=round(self.bytes / 1024 / wall_s, 1), decoded=self.decoded,
                    skipped=self.skipped, errors=self.errors, slots=len(self.slots),
                    decode_cpu_ms_total=round(self.cpu_s * 1000, 1),
                    decode_us_per_frame=round(self.cpu_s * 1e6 / self.frames, 1) if self.frames else None)


async def consume(updates, duration_s, *, max_frames=5_000_000, clock=time.monotonic, stop=None, why=None):
    """Meter `updates` (an async iterator of SubscribeUpdate) until duration_s, max_frames, the
    stream's end, an error or `stop` (SIGTERM or the heartbeat guard; `why["reason"]` names it).
    Each wait is bounded by the time left (Gamma LOW-2). The meters up to that point are always
    returned: an error mid-run is counts["ended"] = "error" with counts["first_error"], never empty
    meters (Gamma info on 137de6e87)."""
    curve, amm = Meter(), Meter()
    counts = dict(slot_frames=0, pongs=0, ended="duration", first_error=None)
    stop = stop or asyncio.Event()
    start = clock()
    iterator = updates.__aiter__()
    halt = asyncio.ensure_future(stop.wait())    # one stop waiter for the whole run (Gamma LOW)
    try:
        return await _consume_loop(iterator, duration_s, max_frames, clock, stop, why, halt, start,
                                   curve, amm, counts)
    finally:
        halt.cancel()


async def _consume_loop(iterator, duration_s, max_frames, clock, stop, why, halt, start, curve, amm, counts):
    while True:
        remaining = duration_s - (clock() - start)
        if remaining <= 0:
            break
        if stop.is_set():
            counts["ended"] = (why or {}).get("reason") or "stopped"
            break
        if curve.frames + amm.frames >= max_frames:
            counts["ended"] = "max_frames"
            break
        nxt = asyncio.ensure_future(iterator.__anext__())
        done, _ = await asyncio.wait({nxt, halt}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        if nxt not in done:
            nxt.cancel()
            continue                          # time up or stopped: the loop head says which
        try:
            update = nxt.result()
        except StopAsyncIteration:
            counts["ended"] = "stream_end"
            break
        except Exception as exc:              # stop on the first error, reported, never retried
            counts["ended"] = "error"
            counts["first_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            break
        if update.HasField("account"):
            curve.add(update, decode_curve, update.account.slot)
        elif update.HasField("transaction"):
            amm.add(update, decode_amm, update.transaction.slot)
        elif update.HasField("slot"):
            counts["slot_frames"] += 1
        elif update.HasField("pong"):
            counts["pongs"] += 1
    return curve, amm, counts


class HeartbeatGuard:
    """Alpha's abort rule for a probe beside the funded runner, read from its heartbeat file once a
    second. The runner is identified by the heartbeat's started_at (it carries no pid).
      - a feed reconnect counter rising under the same runner: abort (feed_reconnect);
      - a stale heartbeat (age > STALE_S, or unreadable) that comes back under a NEW runner: a deploy
        restart, logged; the baseline is re-read from the new runner and the probe keeps running;
      - a stale heartbeat that comes back under the SAME runner, or stays stale past
        RESTART_GRACE_S: abort (heartbeat_gap_same_runner / heartbeat_stale)."""

    STALE_S = 5.0
    RESTART_GRACE_S = 60.0

    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.base = None                  # (started_at, counters)
        self.gap_since = None
        self.events = []

    @staticmethod
    def counters(hb):
        sl = hb.get("slot_lane") or {}
        out = {"slot": sl.get("reconnects") or 0, "preconf": ((sl.get("preconf") or {}).get("feed") or {}).get("reconnects") or 0}
        for name, feed in (sl.get("signal_feeds") or {}).items():
            out["feed:" + name] = (feed or {}).get("reconnects") or 0
        for name, src in (sl.get("preconf_sources") or {}).items():
            out["preconf:" + name] = ((src or {}).get("feed") or {}).get("reconnects") or 0
        return out

    def check(self):
        """None to keep running, else the abort reason."""
        now = self.clock()
        try:
            hb = json.loads(self.path.read_text())
            age, started = now - float(hb["ts"]), hb.get("started_at")
        except Exception:
            hb, age, started = None, None, None
        if hb is None or age > self.STALE_S:
            self.gap_since = self.gap_since or now
            if now - self.gap_since > self.RESTART_GRACE_S:
                return "heartbeat_stale"
            return None
        counts = self.counters(hb)
        if self.base is None:
            self.base = (started, counts)
            return None
        if started != self.base[0]:
            self.events.append(dict(event="deploy_restart", old=self.base[0], new=started,
                                    gap_s=round(now - self.gap_since, 1) if self.gap_since else None))
            self.base, self.gap_since = (started, counts), None
            return None
        if self.gap_since is not None:
            return "heartbeat_gap_same_runner"
        risen = sorted(k for k, v in counts.items() if v > self.base[1].get(k, 0))
        return "feed_reconnect:" + ",".join(risen) if risen else None


async def probe(url, token, duration_s, *, max_frames=5_000_000, guard=None):
    import grpc
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    curve, amm, counts = Meter(), Meter(), dict(slot_frames=0, pongs=0, ended="error", first_error=None)
    start_wall, ru0 = time.monotonic(), resource.getrusage(resource.RUSAGE_SELF)
    stop, why = asyncio.Event(), {}

    def halt(reason):
        why.setdefault("reason", reason)
        stop.set()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, halt, "sigterm")

    async def watch():
        while guard is not None and not stop.is_set():
            reason = guard.check()
            if reason:
                return halt("abort:" + reason)
            await asyncio.sleep(1.0)
    watcher = asyncio.ensure_future(watch())

    async def requests():
        yield subscription()
        n = 0
        while not stop.is_set():
            await asyncio.sleep(5)
            n += 1
            yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=n))

    async with grpc.aio.secure_channel(urlsplit(url).hostname + ":443", grpc.ssl_channel_credentials(),
                                       options=[("grpc.max_receive_message_length", 16 * 1024 * 1024)]) as channel:
        call = channel.stream_stream("/geyser.Geyser/Subscribe",
                                     request_serializer=pb.SubscribeRequest.SerializeToString,
                                     response_deserializer=pb.SubscribeUpdate.FromString)
        stream = call(requests(), metadata=(("x-token", token),))
        try:
            curve, amm, counts = await consume(stream, duration_s, max_frames=max_frames, stop=stop, why=why)
        finally:
            stop.set()
            watcher.cancel()
            stream.cancel()
    wall = max(1e-9, time.monotonic() - start_wall)
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    return dict(endpoint=urlsplit(url).hostname, commitment="processed", wall_s=round(wall, 1),
                curve=curve.report(wall), amm=amm.report(wall), **counts,
                process_cpu_s=round(cpu, 2), process_cpu_pct=round(100 * cpu / wall, 1),
                max_rss_mb=round(ru1.ru_maxrss / 1024, 1),
                guard_events=list(guard.events) if guard is not None else None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--connect", action="store_true",
                    help="open the one Yellowstone connection (only after Alpha confirms a free slot)")
    ap.add_argument("--duration", type=float, default=900.0)
    ap.add_argument("--url", default=None, help="Shyft gRPC URL (default: first MEMECOIN_SHYFT_GRPC_URLS, else fra)")
    ap.add_argument("--heartbeat", default=None,
                    help="the funded runner's heartbeat the abort guard reads (default: <state root>/data/memecoin_runner.heartbeat)")
    args = ap.parse_args(argv)
    if not 0 < args.duration <= 1800:
        ap.error("--duration must be in (0, 1800] seconds")
    from sniper.util.envfiles import load_env_files
    env = dict(os.environ)
    state_root = ROOT if (ROOT / ".env").exists() else Path(os.environ.get("SNIPER_STATE_ROOT", ROOT))
    load_env_files(state_root, env=env)
    url = args.url or (env.get("MEMECOIN_SHYFT_GRPC_URLS") or DEFAULT_URL).split(",")[0].strip()
    host = urlsplit(url).hostname or ""
    if urlsplit(url).scheme != "https" or not host.endswith(".shyft.to"):
        ap.error("--url must be an https *.shyft.to endpoint")
    heartbeat = args.heartbeat or str(state_root / "data" / "memecoin_runner.heartbeat")
    plan = dict(endpoint=host, commitment="processed", duration_s=args.duration, heartbeat=heartbeat,
                filters={"curve": "accounts owner=%s" % PUMP_PROGRAM,
                         "amm": "transactions account_include=%s failed=false vote=false" % PUMP_AMM_PROGRAM})
    if not args.connect:
        print(json.dumps(dict(plan, connected=False, note="plan only; pass --connect once a slot is confirmed")))
        return 0
    if not Path(heartbeat).is_file():         # a guard on a missing file would only time out: refuse
        print(json.dumps(dict(plan, error="heartbeat not found: set SNIPER_STATE_ROOT or --heartbeat")))
        return 2
    token = env.get("MEMECOIN_SHYFT_GRPC_TOKEN")
    if not token:
        print(json.dumps(dict(plan, error="MEMECOIN_SHYFT_GRPC_TOKEN unset")))
        return 2
    out = asyncio.run(probe(url, token, args.duration, guard=HeartbeatGuard(heartbeat)))
    print(json.dumps(dict(plan, **out)), flush=True)
    return 0 if out["ended"] in ("duration", "max_frames") else 1


if __name__ == "__main__":
    sys.exit(main())

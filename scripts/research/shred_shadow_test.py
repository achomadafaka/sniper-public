#!/usr/bin/env python3
"""Bounded read-only feed benchmark on frozen registry owners and one monotonic process clock.

A/A2: identical isolated-research-key Helius processed logs (A2 is the negative control).
H: Helius preprocessed WSS; only its versioned slot/signature prefix is decoded. Execution meta,
transaction index, owner initiation and payment proof remain unknown until confirmed enrichment.
Shyft AMS/FRA beta and Rabbit (B) are opt-in; missing credentials/dependencies are explicit arms.

Readiness starts after subscription ACKs. First receipts survive reconnects; duplicate deliveries,
startup backlog, stale heads and outages cannot become latency samples. H comparisons and their
A2 control use the same common healthy windows. A local slot head never proves landing feasibility.

Message and H delivered-message credit caps close every feed. H bills 0.1 credits per transaction;
the credit estimate excludes baseline/RPC charges and messages already queued at the provider.
No trading, production writes, dependency installation or service changes occur.

    python3 scripts/research/shred_shadow_test.py run --seconds 120 --max-messages 5000 \\
        --max-helius-credits 50 --out <fresh-dir>
    python3 scripts/research/shred_shadow_test.py enrich --dir <dir>
    python3 scripts/research/shred_shadow_test.py summarize --dir <dir>
    python3 scripts/research/shred_shadow_test.py --selftest
"""
import argparse
import atexit
import asyncio
import contextlib
import importlib.util
import json
import math
import os
import re
import sys
import threading
import time
from functools import cache
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

PROTO_DIR = Path(__file__).resolve().parent / "proto" / "yellowstone"
COVERAGE_GRACE_NS = 5_000_000_000
HEAD_FRESH_NS = 2_000_000_000
ACK_TIMEOUT_S = 10.0
MAX_RECEIPT_BODY_BYTES = 4 * 1024 * 1024
FEEDS = ("A", "A2", "H", "SHYFT_AMS", "SHYFT_FRA", "B")


def _positive(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError("%s must be finite and positive" % name)


def _redact(value, secrets=()):
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"(?:https?|wss?)://\S+", "[url]", value)
    return value


def _utc():
    return datetime.now(timezone.utc).isoformat()


class Recorder:
    """Thread-safe JSONL writer plus per-feed byte counters flushed each minute."""

    def __init__(self, path, *, stop=None, max_messages=100000, max_helius_credits=1000,
                 secrets=()):
        _positive(max_messages, "max_messages")
        _positive(max_helius_credits, "max_helius_credits")
        self.helius_limit = int(Decimal(str(max_helius_credits)) * 10)
        if self.helius_limit < 1 or int(max_messages) != max_messages:
            raise ValueError("caps require at least one message and 0.1 Helius credits")
        self.fh = open(path, "x", buffering=1)
        self.lock = threading.Lock()
        self.bw = {}
        self.bw_t_ns = time.monotonic_ns()
        self.stop = stop if stop is not None else threading.Event()
        self.max_messages = max_messages
        self.total_messages = self.helius_messages = 0
        self.stop_reason = None
        self.secrets = secrets
        self.first = set()
        self.duplicates = {}

    def write(self, rec):
        with self.lock:
            self.fh.write(json.dumps(_redact(rec, self.secrets), separators=(",", ":")) + "\n")

    def count(self, feed, nbytes, *, billable=False):
        with self.lock:
            if self.stop_reason:
                return False
            b = self.bw.setdefault(feed, [0, 0])
            b[0] += nbytes
            b[1] += 1
            self.total_messages += 1
            self.helius_messages += int(billable)
            if self.helius_messages >= self.helius_limit:
                self.stop_reason = "helius_credit_cap"
            elif self.total_messages >= self.max_messages:
                self.stop_reason = "message_cap"
            if self.stop_reason:
                self.stop.set()
            return True

    def tx(self, row):
        with self.lock:
            key = (row["feed"], row["sig"])
            if key in self.first:
                feed = row["feed"]
                self.duplicates[feed] = self.duplicates.get(feed, 0) + 1
                return
            self.first.add(key)
        self.write(row)

    def flush_bw(self):
        with self.lock:
            rows, self.bw = self.bw, {}
            now = time.monotonic_ns()
            span_ns, self.bw_t_ns = now - self.bw_t_ns, now
        for feed, (nbytes, msgs) in rows.items():
            self.write({"k": "bw", "feed": feed, "t_ns": now, "bytes": nbytes, "msgs": msgs,
                        "span_ns": span_ns})

    def close(self):
        self.fh.close()


class Head:
    def __init__(self):
        self.slot = self.t_ns = None


def latency_exclusion(slot, t_ns, head, floor_slot):
    if head is None or head.slot is None or head.t_ns is None:
        return "head_unknown"
    if t_ns - head.t_ns > HEAD_FRESH_NS:
        return "head_stale"
    if slot is None:
        return "slot_unknown"
    # Two slots tolerate ordinary processed delivery; older receipts are a censored backlog.
    if floor_slot is None or slot < max(floor_slot, head.slot - 2):
        return "backlog"
    return None


# --- owners and credentials ---------------------------------------------------------------------

def load_owners():
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.memecoin_address_registry import armed_verified_owners
    from sniper.util.state_paths import state_path
    with contextlib.closing(get_readonly_connection(str(state_path("sniper_tracker.db")))) as c:
        owners = armed_verified_owners(c)
    if not owners:
        raise SystemExit("no armed registry owners: nothing to watch")
    return owners


def research_ws_url():
    import slot_lane_send_monitor as mon
    from sniper.services.memecoin_slot_signal import ws_url_from_http
    url = ws_url_from_http(mon.rpc_url())      # refuses a key shared with a funded or watcher URL
    if not url:
        raise SystemExit("research Helius URL did not convert to a WebSocket URL")
    return url


def research_preprocessed_url():
    import slot_lane_send_monitor as mon
    # mon.rpc_url performs key isolation before any endpoint alias is constructed.
    key = parse_qs(urlsplit(mon.rpc_url()).query).get("api-key", [])
    if len(key) != 1 or not key[0]:
        raise SystemExit("isolated research Helius URL has no single api-key")
    return "wss://beta.helius-rpc.com/?" + urlencode({"api-key": key[0]})


def parse_preprocessed(raw):
    import base58
    if not isinstance(raw, bytes) or len(raw) <= 73 or raw[0] != 1:
        raise ValueError("unsupported_or_truncated_preprocessed_frame")
    return {"slot": int.from_bytes(raw[1:9], "little"),
            "sig": base58.b58encode(raw[9:73]).decode(), "owner": None, "err": None,
            "index": None, "metadata": "unknown", "source_readiness": "unknown"}


def shred_credentials(env=None):
    """(url, token) or None. A token that also appears in any non-research variable is refused."""
    env = os.environ if env is None else env
    url = (env.get("MEMECOIN_RESEARCH_SHRED_URL") or "").strip()
    token = (env.get("MEMECOIN_RESEARCH_SHRED_TOKEN") or "").strip()
    if not url or not token:
        return None
    for name, value in env.items():
        if not name.startswith("MEMECOIN_RESEARCH_") and value and token in value:
            raise SystemExit("MEMECOIN_RESEARCH_SHRED_TOKEN also appears in %s; refusing" % name)
    return url, token


def _shyft_missing_deps():
    missing = [n for n in ("grpc", "grpc_tools") if importlib.util.find_spec(n) is None]
    if not all((PROTO_DIR / n).is_file() for n in ("geyser.proto", "solana-storage.proto")):
        missing.append("proto")
    return missing


def _grpc_target(url):
    parsed = urlsplit(url if "://" in url else "https://" + url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or not parsed.hostname.isascii()
            or any(c.isspace() for c in url)):
        raise ValueError("endpoint_invalid")
    port = 443 if parsed.port is None else parsed.port
    if port == 0:
        raise ValueError("endpoint_invalid")
    host = "[%s]" % parsed.hostname if ":" in parsed.hostname else parsed.hostname
    return "%s:%s" % (host, port)


def optional_arms(env=None, *, enabled=False, missing_deps=None):
    env = os.environ if env is None else env
    if missing_deps is None:
        missing_deps = _shyft_missing_deps()
    token = (env.get("MEMECOIN_RESEARCH_SHYFT_TOKEN") or "").strip()
    if token:
        for name, value in env.items():
            if not name.startswith("MEMECOIN_RESEARCH_") and value and token in value:
                raise SystemExit("research Shyft token also appears in %s; refusing" % name)
    legacy = shred_credentials(env)
    arms = []
    for feed, url in (("SHYFT_AMS", env.get("MEMECOIN_RESEARCH_SHYFT_AMS_URL") or "https://beta-grpc.ams.shyft.to"),
                      ("SHYFT_FRA", env.get("MEMECOIN_RESEARCH_SHYFT_FRA_URL") or "https://beta-grpc.fra.shyft.to"),
                      ("B", env.get("MEMECOIN_RESEARCH_RABBIT_URL") or "https://rabbitstream.ams.shyft.to/")):
        arm_token = token
        if feed == "B" and legacy:
            url, arm_token = legacy
        active = enabled or (feed == "B" and bool(legacy))
        why = "not_opted_in" if not active else ("token_missing" if not arm_token else None)
        if why is None and missing_deps:
            why = "dependencies_missing:" + ",".join(missing_deps)
        try:
            _grpc_target(url)
        except ValueError:
            url = None  # Invalid inactive arms must not break baseline endpoint diagnostics.
            if why is None:
                why = "endpoint_invalid"
        arms.append({"feed": feed, "url": url, "token": arm_token,
                     "status": "configured" if why is None else "unavailable", "why": why,
                     "from_slot": None})
    return arms


def shyft_check(env=None):
    """Offline setup check; readiness does not claim provider authentication or latency."""
    missing = _shyft_missing_deps()
    arms = optional_arms(env, enabled=True, missing_deps=missing)
    protocol = "dependencies_missing:" + ",".join(missing) if missing else "ready"
    if not missing:
        try:
            _yellowstone_modules()
        except (Exception, SystemExit) as exc:
            protocol = "protocol_error:" + type(exc).__name__
    return {"ready": protocol == "ready" and all(a["status"] == "configured" for a in arms),
            "protocol": protocol, "network_checked": False, "funded_enabled": False,
            "arms": [{"feed": a["feed"], "status": a["status"], "why": a["why"],
                      "token_present": bool(a["token"])} for a in arms]}


# --- WebSocket feeds: processed A/A2 and pre-execution H ------------------------------------------

async def _ws_feed(name, ws_url, owners, rec, stop, head, *, early=False):
    import websockets
    backoff = 1.0
    while not stop.is_set():
        why = "stop"
        if name == "A" and head is not None:
            head.slot = head.t_ns = None
        try:
            async with websockets.connect(ws_url, max_size=1024 * 1024, max_queue=1,
                                          open_timeout=5, close_timeout=1, ping_interval=20) as ws:
                req_owner, sub_owner = {}, {}
                if early:
                    pending = {1}
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                        "method": "preprocessedSubscribe", "params": {"accountInclude": sorted(owners)}}))
                else:
                    req_owner = dict(enumerate(sorted(owners), start=1))
                    pending = set(req_owner)
                    for ident, owner in req_owner.items():
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": ident,
                            "method": "logsSubscribe", "params": [{"mentions": [owner]},
                                                                  {"commitment": "processed"}]}))
                    if name == "A" and head is not None:
                        pending.add(0)
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "slotSubscribe"}))
                ack_deadline = time.monotonic() + ACK_TIMEOUT_S
                ready, floor_slot = False, None
                while not stop.is_set():
                    if not ready and time.monotonic() >= ack_deadline:
                        raise TimeoutError("subscription_ack_timeout")
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
                        t_ns = time.monotonic_ns()  # first action after receipt, before parsing
                    except asyncio.TimeoutError:
                        continue
                    size = len(raw) if isinstance(raw, bytes) else len(raw.encode())
                    if not rec.count(name, size, billable=early and isinstance(raw, bytes)):
                        break
                    if early and isinstance(raw, bytes):
                        row = parse_preprocessed(raw)
                    else:
                        msg = json.loads(raw)
                        ident = msg.get("id")
                        if ident in pending:
                            if msg.get("error") or type(msg.get("result")) is not int or msg["result"] < 0:
                                raise ValueError("subscription_rejected")
                            pending.remove(ident)
                            if ident in req_owner:
                                sub_owner[msg["result"]] = req_owner[ident]
                            if not pending:
                                ready, floor_slot = True, head.slot if head is not None else None
                                rec.write({"k": "up", "feed": name, "t_ns": t_ns, "utc": _utc(),
                                           "owners": len(owners), "readiness": "subscription_ack"})
                                backoff = 1.0
                            continue
                        params = msg.get("params") or {}
                        if msg.get("method") == "slotNotification" and name == "A" and head is not None:
                            slot = (params.get("result") or {}).get("slot")
                            if 0 not in pending and isinstance(slot, int) and (head.slot is None or slot > head.slot):
                                head.slot, head.t_ns = slot, t_ns
                            continue
                        if early or msg.get("method") != "logsNotification":
                            continue
                        res = params.get("result") or {}
                        val = res.get("value") or {}
                        if not val.get("signature"):
                            continue
                        row = {"sig": val["signature"], "slot": (res.get("context") or {}).get("slot"),
                               "owner": sub_owner.get(params.get("subscription")),
                               "err": val["err"] is not None if "err" in val else None,
                               "index": None, "metadata": "processed_logs",
                               "source_readiness": "requires_confirmed_receipt"}
                    if ready and floor_slot is None and head is not None:
                        floor_slot = head.slot
                    excluded = "subscription_not_ready" if not ready else latency_exclusion(
                        row["slot"], t_ns, head, floor_slot)
                    rec.tx(dict(row, k="tx", feed=name, t_ns=t_ns, utc=_utc(), bytes=size,
                                head=head.slot if head is not None else None,
                                head_t_ns=head.t_ns if head is not None else None,
                                latency_eligible=excluded is None, latency_exclusion=excluded))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            why = "%s: %s" % (type(exc).__name__, _redact(str(exc), rec.secrets)[:160])
        finally:
            if name == "A" and head is not None:
                head.slot = head.t_ns = None
            rec.write({"k": "down", "feed": name, "t_ns": time.monotonic_ns(), "utc": _utc(), "why": why})
        if not stop.is_set():
            until = time.monotonic() + backoff
            while not stop.is_set() and time.monotonic() < until:
                await asyncio.sleep(0.1)
            backoff = min(backoff * 2, 30.0)


async def helius_feed(name, ws_url, owners, rec, stop, head=None):
    await _ws_feed(name, ws_url, owners, rec, stop, head)


async def preprocessed_feed(ws_url, owners, rec, stop, head):
    await _ws_feed("H", ws_url, owners, rec, stop, head, early=True)


# --- feed B: Yellowstone-compatible gRPC with a server-side owner filter -------------------------

@cache
def _yellowstone_modules():
    """(geyser_pb2, geyser_pb2_grpc) compiled from the vendored protos into a temp dir."""
    import tempfile
    import shutil
    from grpc_tools import protoc
    if not (PROTO_DIR / "geyser.proto").exists():
        raise SystemExit("missing %s/geyser.proto (and solana-storage.proto)" % PROTO_DIR)
    out = tempfile.mkdtemp(prefix="yellowstone_pb_")
    atexit.register(shutil.rmtree, out, ignore_errors=True)
    import grpc_tools
    include = str(Path(grpc_tools.__file__).parent / "_proto")
    rc = protoc.main(["protoc", "-I%s" % PROTO_DIR, "-I%s" % include, "--python_out=%s" % out,
                      "--grpc_python_out=%s" % out, str(PROTO_DIR / "geyser.proto"),
                      str(PROTO_DIR / "solana-storage.proto")])
    if rc != 0:
        raise SystemExit("protoc failed (%d)" % rc)
    sys.path.insert(0, out)
    import geyser_pb2
    import geyser_pb2_grpc
    return geyser_pb2, geyser_pb2_grpc


def shred_feed(url, token, owners, rec, stop, name="B", head=None):
    import grpc
    import base58
    pb, pb_grpc = _yellowstone_modules()
    host = _grpc_target(url)
    owner_set = set(owners)

    def requests():
        # No from_slot: a replay is evidence of recovery, never a latency sample.
        yield pb.SubscribeRequest(
            transactions={"owners": pb.SubscribeRequestFilterTransactions(
                vote=False, account_include=sorted(owners))},
            commitment=pb.CommitmentLevel.PROCESSED)
        yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1))
        n = 1
        while not stop.wait(10):
            n += 1
            yield pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=n))

    backoff = 1.0
    while not stop.is_set():
        channel = stream = None
        finished = threading.Event()
        acknowledged = threading.Event()
        ack_timed_out = threading.Event()
        canceller = None
        why = "stop"
        try:
            channel = grpc.secure_channel(host, grpc.ssl_channel_credentials(), options=[
                ("grpc.max_receive_message_length", 1024 * 1024), ("grpc.keepalive_time_ms", 10000)])
            stream = pb_grpc.GeyserStub(channel).Subscribe(requests(), metadata=(("x-token", token),))
            ack_deadline = time.monotonic() + ACK_TIMEOUT_S
            def cancel_on_stop():
                while not finished.wait(0.05):
                    if not acknowledged.is_set() and time.monotonic() >= ack_deadline:
                        ack_timed_out.set()
                    if stop.is_set():
                        stream.cancel()
                        channel.close()
                        return
                    if ack_timed_out.is_set():
                        stream.cancel()
                        channel.close()
                        return
            canceller = threading.Thread(target=cancel_on_stop, daemon=True)
            canceller.start()
            ready, floor_slot = False, None
            for upd in stream:
                t_ns = time.monotonic_ns()
                size = upd.ByteSize()
                if not rec.count(name, size):
                    break
                if upd.HasField("pong") and upd.pong.id == 1 and not ready:
                    ready, floor_slot = True, head.slot if head is not None else None
                    acknowledged.set()
                    rec.write({"k": "up", "feed": name, "t_ns": t_ns, "utc": _utc(),
                               "owners": len(owners), "readiness": "subscription_pong"})
                    backoff = 1.0
                    continue
                if not ready and time.monotonic() >= ack_deadline:
                    raise TimeoutError("subscription_ack_timeout")
                if not upd.HasField("transaction"):
                    continue
                info = upd.transaction.transaction
                if (len(info.signature) != 64 or upd.transaction.slot == 0
                        or not info.transaction.message.account_keys
                        or any(len(k) != 32 for k in info.transaction.message.account_keys)):
                    raise ValueError("invalid_transaction_identity")
                keys = [base58.b58encode(k).decode() for k in info.transaction.message.account_keys]
                hit = sorted(owner_set.intersection(keys))
                if ready and floor_slot is None and head is not None:
                    floor_slot = head.slot
                excluded = "subscription_not_ready" if not ready else latency_exclusion(
                    upd.transaction.slot, t_ns, head, floor_slot)
                rec.tx({"k": "tx", "feed": name, "sig": base58.b58encode(info.signature).decode(),
                        "slot": upd.transaction.slot, "index": None, "err": None,
                        "metadata": "unknown", "source_readiness": "unknown",
                        "owner": hit[0] if hit else None, "t_ns": t_ns, "utc": _utc(),
                        "head": head.slot if head is not None else None,
                        "head_t_ns": head.t_ns if head is not None else None,
                        "bytes": size, "latency_eligible": excluded is None,
                        "latency_exclusion": excluded})
                if stop.is_set():
                    break
            if ack_timed_out.is_set():
                raise TimeoutError("subscription_ack_timeout")
        except Exception as exc:
            why = "subscription_ack_timeout" if ack_timed_out.is_set() else (
                "%s: %s" % (type(exc).__name__, _redact(str(exc), rec.secrets)[:160]))
        finally:
            finished.set()
            if stream is not None:
                stream.cancel()
            if channel is not None:
                channel.close()
            if canceller is not None:
                canceller.join(timeout=1)
            rec.write({"k": "down", "feed": name, "t_ns": time.monotonic_ns(), "utc": _utc(), "why": why})
        stop.wait(backoff)
        backoff = min(backoff * 2, 30)


def run(hours, out, with_shred=True, *, with_shyft=False, max_messages=100000,
        max_helius_credits=1000):
    _positive(hours, "hours")
    _positive(hours * 3600, "duration_seconds")
    _positive(max_messages, "max_messages")
    _positive(max_helius_credits, "max_helius_credits")
    if int(max_messages) != max_messages or max_helius_credits < 0.1:
        raise ValueError("caps require integral messages and at least 0.1 Helius credits")
    if out is None:
        raise ValueError("a fresh output directory is required")
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError("output directory already contains an experiment")
    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))
    owners = load_owners()
    ws_url, early_url = research_ws_url(), research_preprocessed_url()
    arms = optional_arms(enabled=with_shyft)
    if not with_shred:
        for arm in arms:
            arm.update(status="unavailable", why="not_opted_in")
    if any(a["status"] == "configured" for a in arms):
        # Compile/import once on the main thread, before feed workers touch sys.path.
        _yellowstone_modules()
    out.mkdir(parents=True, exist_ok=True)
    stop, head = threading.Event(), Head()
    secrets = tuple([parse_qs(urlsplit(ws_url).query).get("api-key", [""])[0]]
                    + [a["token"] for a in arms if a["token"]])
    rec = Recorder(out / "events.jsonl", stop=stop, max_messages=max_messages,
                   max_helius_credits=max_helius_credits, secrets=secrets)
    rec.write({"k": "start", "utc": _utc(), "t_ns": time.monotonic_ns(), "owners": sorted(owners),
               "hours": hours, "max_messages": max_messages, "max_helius_credits": max_helius_credits,
               "credit_scope": "H binary deliveries only; baseline and enrichment excluded"})
    for name in ("A", "A2", "H"):
        rec.write({"k": "arm", "feed": name, "status": "configured", "t_ns": time.monotonic_ns()})
    for arm in arms:
        rec.write({"k": "arm", "feed": arm["feed"], "status": arm["status"], "why": arm["why"],
                   "endpoint_host": urlsplit(arm["url"] or "").hostname,
                   "from_slot": None, "t_ns": time.monotonic_ns()})
    threads = [threading.Thread(target=shred_feed,
               args=(a["url"], a["token"], owners, rec, stop, a["feed"], head), daemon=True)
               for a in arms if a["status"] == "configured"]
    deadline = time.monotonic() + hours * 3600

    async def main():
        feeds = [asyncio.create_task(helius_feed("A", ws_url, owners, rec, stop, head)),
                 asyncio.create_task(helius_feed("A2", ws_url, owners, rec, stop, head)),
                 asyncio.create_task(preprocessed_feed(early_url, owners, rec, stop, head))]
        next_flush = time.monotonic() + 60
        try:
            for thread in threads:
                thread.start()
            while not stop.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
                if time.monotonic() >= next_flush:
                    rec.flush_bw()
                    next_flush = time.monotonic() + 60
        finally:
            stop.set()
            for feed in feeds:
                feed.cancel()
            await asyncio.gather(*feeds, return_exceptions=True)

    try:
        asyncio.run(main())
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        rec.flush_bw()
        rec.write({"k": "end", "utc": _utc(), "t_ns": time.monotonic_ns(),
                   "why": rec.stop_reason or "duration_or_interrupt", "duplicates": rec.duplicates,
                   "messages": rec.total_messages, "helius_messages": rec.helius_messages,
                   "helius_credits_estimate": rec.helius_messages / 10,
                   "threads_still_alive": sum(t.is_alive() for t in threads)})
        rec.close()


# --- enrichment: what each signature was ----------------------------------------------------------

def book_is_flat():
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.state_paths import state_path
    with contextlib.closing(get_readonly_connection(str(state_path("sniper_tracker.db")))) as conn:
        positions = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                                 "AND network_id='solana' AND is_open=1 "
                                 "AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]
        reserved = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
                                "AND COALESCE(network_id,'solana')='solana'").fetchone()[0]
    return positions == 0 and reserved == 0


def read_receipt(url, sig, deadline):
    import aiohttp
    from sniper.services.memecoin_research_pacer import SharedPacer
    pacer = SharedPacer.for_url(url)
    def bounded_sleep(delay):
        if time.monotonic() + delay >= deadline:
            raise TimeoutError("enrichment_deadline")
        time.sleep(delay)
    pacer.sleep = bounded_sleep  # this private instance keeps the existing cross-process pacing
    pacer.acquire()
    if not book_is_flat():
        raise RuntimeError("funded_book_open")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("enrichment_deadline")
    async def fetch():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("enrichment_deadline")
        timeout = aiohttp.ClientTimeout(total=remaining, ceil_threshold=math.inf)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [sig,
            {"encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}]}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, allow_redirects=False) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(body) + len(chunk) > MAX_RECEIPT_BODY_BYTES:
                        raise ValueError("receipt_body_too_large")
                    body.extend(chunk)
                result = json.loads(body)
                if time.monotonic() >= deadline:
                    raise TimeoutError("enrichment_deadline")
                return result
    result = asyncio.run(fetch())
    if result.get("error"):
        raise ValueError("receipt_rpc_error")
    return result.get("result")


def enrichment_row(sig, tx, owners):
    row = {"landed": None, "receipt_state": "unknown", "slot": None, "err": None,
           "lane_buy": None, "venue": None, "index": None, "source_readiness": "unknown"}
    if not isinstance(tx, dict) or not isinstance(tx.get("slot"), int):
        return row
    transaction = tx.get("transaction")
    signatures = transaction.get("signatures") if isinstance(transaction, dict) else None
    if not isinstance(signatures, list) or not signatures or any(
            not isinstance(signature, str) or not signature for signature in signatures):
        row["read_reason"] = "signature_missing_or_invalid"
        return row
    if sig != signatures[0]:
        row["read_reason"] = "signature_mismatch"
        return row
    row.update(landed=True, receipt_state="confirmed", slot=tx["slot"])
    meta = tx.get("meta")
    if not isinstance(meta, dict) or "err" not in meta or not isinstance(meta.get("logMessages"), list):
        row["receipt_state"] = "partial"
        return row
    row["err"] = meta["err"] is not None
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys")
    header = message.get("header")
    count = header.get("numRequiredSignatures") if isinstance(header, dict) else None
    if (not isinstance(keys, list) or not keys or type(count) is not int
            or not 1 <= count <= len(keys) or count != len(signatures)):
        row["receipt_state"] = "partial"
        return row
    signers = {k.get("pubkey") for k in keys if isinstance(k, dict) and k.get("signer")}
    if keys and isinstance(keys[0], str):
        signers = set(keys[:count])
    hits = sorted(signers.intersection(owners))
    if not hits:
        row["lane_buy"] = False
        row["source_readiness"] = "no_frozen_owner_signer"
        return row
    from sniper.services.memecoin_slot_signal import decode_owner_buy, _owner_bytes
    frame = {"params": {"result": {"context": {"slot": tx["slot"]}, "value": {
        "signature": sig, "err": meta["err"], "logs": meta["logMessages"]}}}}
    buys = [decode_owner_buy(frame, owner, _owner_bytes(owner), "research",
                             recv_monotonic=0, recv_utc="") for owner in hits]
    buy = next((b for b in buys if b and not b.refusal), None)
    row.update(lane_buy=bool(buy), venue=buy.venue if buy else None,
               source_readiness="confirmed_signer_bound_buy" if buy else "no_lane_buy")
    return row


def enrich(out, *, max_rpc_calls=100, seconds=120):
    import aiohttp
    import slot_lane_send_monitor as mon
    import urllib.error
    _positive(max_rpc_calls, "max_rpc_calls")
    _positive(seconds, "seconds")
    if int(max_rpc_calls) != max_rpc_calls:
        raise ValueError("max_rpc_calls must be integral")
    events = [json.loads(line) for line in open(Path(out) / "events.jsonl")]
    owners = set(next((e.get("owners", []) for e in events if e.get("k") == "start"), []))
    owners.update(e["owner"] for e in events if e.get("k") == "tx" and e.get("owner"))
    signatures = dict.fromkeys(e["sig"] for e in events if e.get("k") == "tx")
    path = Path(out) / "enriched.json"
    done = json.loads(path.read_text()) if path.exists() else {}
    url, deadline = mon.rpc_url(), time.monotonic() + seconds
    calls, stopped = 0, None
    for sig in signatures:
        if done.get(sig, {}).get("receipt_state") == "confirmed":
            continue
        row = enrichment_row(sig, None, owners)
        if stopped is None:
            if calls >= max_rpc_calls:
                stopped = "rpc_call_cap"
            elif time.monotonic() >= deadline:
                stopped = "enrichment_deadline"
            elif not book_is_flat():
                stopped = "funded_book_open"
        if stopped is None:
            calls += 1
            try:
                row = enrichment_row(sig, read_receipt(url, sig, deadline), owners)
                if row["landed"] is None:
                    row["read_reason"] = row.get("read_reason", "null_receipt")
            except urllib.error.HTTPError as exc:
                stopped = "http_%d" % exc.code
            except aiohttp.ClientResponseError as exc:
                stopped = "http_%d" % exc.status
            except Exception as exc:
                stopped = type(exc).__name__
        if stopped:
            row["read_reason"] = stopped
        done[sig] = row
    path.write_text(json.dumps(done))
    (Path(out) / "enrichment_status.json").write_text(json.dumps(
        {"rpc_calls": calls, "stop_reason": stopped, "seconds_bound": seconds,
         "max_rpc_calls": max_rpc_calls, "unknown": sum(r.get("landed") is None for r in done.values())}))
    print("enriched %d signatures; RPC calls %d; stop %s" % (len(done), calls, stopped), file=sys.stderr)


# --- summary --------------------------------------------------------------------------------------

def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else None


def up_windows(events, feed):
    """[(start_ns, end_ns)] when `feed` was connected."""
    out, start = [], None
    for e in sorted(events, key=lambda row: row.get("t_ns", 0)):
        if e.get("feed") != feed or e.get("k") not in ("up", "down"):
            continue
        if e["k"] == "up" and start is None:
            start = e["t_ns"]
        elif e["k"] == "down" and start is not None:
            out.append((start, e["t_ns"]))
            start = None
    if start is not None:
        end = max((e.get("t_ns") or 0) for e in events)
        out.append((start, end))
    return out


def _was_up(windows, t_ns):
    return any(a <= t_ns - COVERAGE_GRACE_NS and t_ns + COVERAGE_GRACE_NS <= b for a, b in windows)


def _common_windows(windows, feeds):
    common = windows[feeds[0]]
    for feed in feeds[1:]:
        common = [(max(a, c), min(b, d)) for a, b in common for c, d in windows[feed]
                  if max(a, c) < min(b, d)]
    return common


def _in_one_window(windows, *receipts):
    times = [r["t_ns"] for r in receipts]
    return all(r.get("latency_eligible", True) for r in receipts) and any(
        a + COVERAGE_GRACE_NS <= min(times) and max(times) + COVERAGE_GRACE_NS <= b
        for a, b in windows)


def _comparison(by_sig, other, common):
    pairs = [(s, f["A"], f[other]) for s, f in by_sig.items() if "A" in f and other in f
             and _in_one_window(common, f["A"], f[other])]
    lead_ms = [(a["t_ns"] - o["t_ns"]) / 1e6 for _s, a, o in pairs]
    lead_slots = [a["head"] - o["head"] for _s, a, o in pairs
                  if a.get("head") is not None and o.get("head") is not None]
    eligible_a = [s for s, f in by_sig.items() if "A" in f and _in_one_window(common, f["A"])]
    eligible_other = [s for s, f in by_sig.items() if other in f and _in_one_window(common, f[other])]
    return {"paired": len(pairs), "latency_result": "measured" if pairs else "unknown",
            "lead_ms_p10_p50_p90": [_pct(lead_ms, q) for q in (0.1, 0.5, 0.9)],
            "lead_head_slots_p50_p90": [_pct(lead_slots, q) for q in (0.5, 0.9)],
            "eligible_A": len(eligible_a), "eligible_other": len(eligible_other),
            "seen_by_A_missed_while_up": len(eligible_a) - len(pairs),
            "seen_missed_by_A_while_up": len(eligible_other) - len(pairs),
            "common_healthy_seconds": sum(max(0, b - a - 2 * COVERAGE_GRACE_NS)
                                          for a, b in common) / 1e9}


def summarize_events(events, enriched=None):
    enriched = enriched or {}
    first, duplicates = {}, {}
    for e in events:
        if e.get("k") == "tx":
            key = (e["feed"], e["sig"])
            if key in first:
                duplicates[e["feed"]] = duplicates.get(e["feed"], 0) + 1
            if key not in first or e["t_ns"] < first[key]["t_ns"]:
                first[key] = e
    by_sig = {}
    for (feed, sig), e in first.items():
        by_sig.setdefault(sig, {})[feed] = e
    feeds = tuple(dict.fromkeys(FEEDS + tuple(e["feed"] for e in events if e.get("feed"))))
    windows = {f: up_windows(events, f) for f in feeds}
    out = {}
    for other in feeds:
        if other == "A":
            continue
        required = ("A", "A2") if other == "A2" else ("A", "A2", other)
        common = _common_windows(windows, required)
        out[other] = _comparison(by_sig, other, common)
        if other != "A2":
            out[other]["negative_control"] = _comparison(by_sig, "A2", common)
    buys = [s for s, r in enriched.items() if r.get("lane_buy") and isinstance(r.get("slot"), int)]
    during_s = {}
    for feed in feeds:
        seen = [by_sig[s][feed] for s in buys if s in by_sig and feed in by_sig[s]
                and _in_one_window(windows[feed], by_sig[s][feed])]
        during_s[feed] = {"seen": len(seen), "head_at_or_before_S": sum(
            e.get("head") is not None and e["head"] <= enriched[e["sig"]]["slot"] for e in seen)}
    converted = {}
    for other in feeds:
        if other in ("A", "A2"):
            continue
        common = _common_windows(windows, ("A", "A2", other))
        converted[other] = sum(1 for sig in buys if sig in by_sig and "A" in by_sig[sig]
            and other in by_sig[sig] and _in_one_window(common, by_sig[sig]["A"], by_sig[sig][other])
            and by_sig[sig][other].get("head") is not None and by_sig[sig]["A"].get("head") is not None
            and by_sig[sig][other]["head"] <= enriched[sig]["slot"] < by_sig[sig]["A"]["head"])
    out["lane_buys"] = {"n": len(buys), "during_S": during_s,
                        "seen_at_local_head_S_while_A_after": converted,
                        "B_seen_at_local_head_S_while_A_after": converted.get("B", 0)}
    out["uptime_hours"] = {f: round(sum(b - a for a, b in w) / 3.6e12, 4) for f, w in windows.items()}
    out["arm_availability"] = {e["feed"]: {"status": e["status"], "why": e.get("why")}
                               for e in events if e.get("k") == "arm"}
    out["first_receipts"] = {f: sum(feed == f for feed, _sig in first) for f in feeds}
    out["latency_excluded"] = {f: sum(e.get("latency_eligible") is False for e in first.values()
                                     if e["feed"] == f) for f in feeds}
    end = next((e for e in reversed(events) if e.get("k") == "end"), {})
    out["duplicates"] = end.get("duplicates", duplicates)
    out["stop_reason"] = end.get("why", "end_unknown")
    out["helius_credits_estimate"] = end.get("helius_credits_estimate")
    out["receipt_unknown"] = sum(enriched.get(sig, {}).get("landed") is None for sig in by_sig)
    out["source_ready_buys"] = sum(r.get("source_readiness") == "confirmed_signer_bound_buy"
                                   for r in enriched.values())
    out["legacy_readiness_unverified"] = any("latency_eligible" not in e for e in first.values())
    bw = {}
    for e in events:
        if e.get("k") == "bw":
            span = e.get("span_ns", 60_000_000_000) / 1e9
            if span > 0:
                bw.setdefault(e["feed"], []).append(e["bytes"] / span)
    out["bytes_per_s_p50_max"] = {f: [round(_pct(v, 0.5) or 0), round(max(v))] for f, v in bw.items()}
    return out


def summarize(out):
    events = [json.loads(line) for line in open(Path(out) / "events.jsonl")]
    path = Path(out) / "enriched.json"
    enriched = json.loads(path.read_text()) if path.exists() else {}
    print(json.dumps(summarize_events(events, enriched), indent=1))


# --- selftest -------------------------------------------------------------------------------------

def selftest():
    s = 1_000_000_000
    ev = [{"k": "up", "feed": f, "t_ns": 0} for f in ("A", "A2", "B")]
    # sig1: B 150 ms before A, B while head==S, A after; sig2: A2 = A (control); sig3 only A while B up
    ev += [{"k": "tx", "feed": "B", "sig": "s1", "t_ns": 100 * s, "head": 10},
           {"k": "tx", "feed": "A", "sig": "s1", "t_ns": 100 * s + 150_000_000, "head": 11},
           {"k": "tx", "feed": "A2", "sig": "s1", "t_ns": 100 * s + 150_000_000, "head": 11},
           {"k": "tx", "feed": "A", "sig": "s3", "t_ns": 200 * s, "head": 20},
           {"k": "tx", "feed": "A2", "sig": "s3", "t_ns": 200 * s, "head": 20},
           {"k": "down", "feed": "B", "t_ns": 300 * s},
           # sig4 arrives on A while B is down: not a coverage miss
           {"k": "tx", "feed": "A", "sig": "s4", "t_ns": 301 * s, "head": 30},
           {"k": "up", "feed": "B", "t_ns": 400 * s},
           {"k": "down", "feed": "A", "t_ns": 500 * s}, {"k": "down", "feed": "A2", "t_ns": 500 * s},
           {"k": "down", "feed": "B", "t_ns": 500 * s}]
    r = summarize_events(ev, {"s1": {"lane_buy": True, "slot": 10}})
    assert r["B"]["lead_ms_p10_p50_p90"][1] == 150.0, r["B"]
    assert r["B"]["lead_head_slots_p50_p90"][0] == 1
    assert r["B"]["seen_by_A_missed_while_up"] == 1, "s3 was missed by B while up; s4 fell in its outage"
    assert r["A2"]["lead_ms_p10_p50_p90"][1] == 0.0, "the control must read zero"
    assert r["lane_buys"]["B_seen_at_local_head_S_while_A_after"] == 1
    assert r["uptime_hours"]["B"] == round((300 + 100) * s / 3.6e12, 4)
    with contextlib.suppress(SystemExit):
        shred_credentials({"MEMECOIN_RESEARCH_SHRED_URL": "u", "MEMECOIN_RESEARCH_SHRED_TOKEN": "tok",
                           "MEMECOIN_SOLANA_RPC_URL": "https://x/?api-key=tok"})
        raise AssertionError("a token shared with a funded variable must be refused")
    assert shred_credentials({"MEMECOIN_RESEARCH_SHRED_URL": "u"}) is None
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=("check", "run", "enrich", "summarize"))
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--seconds", type=float, help="run duration or enrichment deadline, in seconds")
    ap.add_argument("--out", "--dir", dest="out")
    ap.add_argument("--max-messages", type=int, default=100000)
    ap.add_argument("--max-helius-credits", type=float, default=1000)
    ap.add_argument("--max-rpc-calls", type=int, default=100)
    ap.add_argument("--with-shyft", action="store_true")
    ap.add_argument("--no-shred", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.cmd == "check":
        from sniper.util.envfiles import load_env_files
        load_env_files(str(REPO))
        report = shyft_check()
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["ready"] else 1)
    elif a.cmd == "run":
        run(a.seconds / 3600 if a.seconds is not None else a.hours, a.out,
            with_shred=not a.no_shred, with_shyft=a.with_shyft,
            max_messages=a.max_messages, max_helius_credits=a.max_helius_credits)
    elif a.cmd == "enrich":
        enrich(a.out, max_rpc_calls=a.max_rpc_calls, seconds=a.seconds if a.seconds is not None else 120)
    elif a.cmd == "summarize":
        summarize(a.out)


if __name__ == "__main__":
    main()

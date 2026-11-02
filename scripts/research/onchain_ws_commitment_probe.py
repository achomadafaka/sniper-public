#!/usr/bin/env python3
"""Does `logsSubscribe` at `processed` beat `confirmed` for the roster owners, and does the edge
survive `getTransaction`?

WHY. `scripts/research/onchain_wallet_watch.py:342` subscribes at `{"commitment": "confirmed"}`
while its own docstring claims `processed`. Gamma measured that watcher losing to the J7 relay on
12 of 12 mints, median 0.53 s, and the 2026-09-22 raw-socket test that BEAT J7 (+0.45 s median,
7 of 7) ran at `processed`. Either the commitment costs us the lead, or the lead is spent later,
inside `getTransaction`. This probe measures both halves on the same signatures.

WHAT IT DOES. One process, two WebSocket sockets on the SAME endpoint: one subscribes every owner
at `processed`, the other subscribes every owner at `confirmed`. Every frame is logged with its
socket's commitment, host monotonic and wall arrival, and raw length. Each socket's first sighting
of a signature then triggers ITS OWN `getTransaction` at ITS OWN commitment, with the watcher's
null-retry, and the time to a parsed and to an actionable result is logged per commitment. The
`confirmed` side is the control: it is the live watcher's path.

TWO SOCKETS, NOT TWO PER OWNER. 27 concurrent connections to this host returned HTTP 429 on all
but one (2026-09-22, `memecoin_ws_byte_budget.py`); a single socket carrying 27 `logsSubscribe`
subscriptions acked 27 of 27. So each commitment gets ONE socket carrying every owner, and
`--max-sockets` is the ceiling this refuses to cross, not a target.

⚠️ THIS RUNS ON THE LIVE WATCHER'S OWN KEY (`ONCHAIN_WATCH_HELIUS_RPC_URL`), by instruction. That
key feeds the funded on-chain entry lane. A leaders scan on the research key once 429'd the process
sharing it, so this probe paces its `getTransaction` calls, counts every 429, and ABORTS on
`--max-429`. A 429 storm here degrades the lane the probe exists to improve.

  python3 scripts/research/onchain_ws_commitment_probe.py --duration 3600
  python3 scripts/research/onchain_ws_commitment_probe.py --selftest

Output: append-only JSONL at `data/onchain_ws_commitment_probe.jsonl`. No ledger writes.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sniper.persistence.db_core import get_readonly_connection  # noqa: E402

DB = REPO / "sniper_tracker.db"
OUT = REPO / "data" / "onchain_ws_commitment_probe.jsonl"
COMMITMENTS = ("processed", "confirmed")
# The SOCKETS carry one commitment each. The READ is always `confirmed`: getTransaction refuses
# anything below it with -32602, so a `processed` read is not a faster read, it is no read.
RPC_COMMITMENT = "confirmed"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decode_rpc(payload):
    """(result, error) for one JSON-RPC reply. An `error` NEVER reads as a null result."""
    if not isinstance(payload, dict):
        return None, {"code": None, "message": f"non-object reply {type(payload).__name__}"}
    err = payload.get("error")
    if err is not None:
        if isinstance(err, dict):
            return None, {"code": err.get("code"), "message": str(err.get("message"))[:160]}
        return None, {"code": None, "message": str(err)[:160]}
    return payload.get("result"), None


def endpoint(env_name: str) -> tuple[str, str]:
    """(http, wss) for the named env var. Never the custody endpoint."""
    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))
    url = (os.getenv(env_name) or "").strip()
    if not url:
        raise SystemExit(f"{env_name} unset; refusing to guess an endpoint")
    custody = (os.getenv("MEMECOIN_SOLANA_RPC_URL") or "").strip()
    if custody and url == custody:
        raise SystemExit("endpoint equals the funded custody URL; refusing")
    return url, url.replace("https://", "wss://", 1)


def owners(limit: int | None = None) -> dict[str, str]:
    """The same confirmed Solana addresses the watcher subscribes (its own source list)."""
    with contextlib.closing(get_readonly_connection(str(DB))) as conn:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT address, handle FROM memecoin_wallet_addresses WHERE network_id='solana' "
            "AND verify_status IN ('fomoscan_confirmed','live_confirmed') ORDER BY address"
        ).fetchall()
    out = {r[0]: (r[1] or r[0][:8]) for r in rows}
    if limit is not None:
        out = dict(list(out.items())[:limit])
    return out


class Probe:
    def __init__(self, http_url: str, out_path: Path, max_rps: float, max_429: int):
        self.http_url = http_url
        self.fh = open(out_path, "a")
        self.min_gap = 1.0 / max_rps if max_rps > 0 else 0.0
        self.max_429 = max_429
        self.n_429 = 0
        self.next_slot = 0.0
        self.gate = asyncio.Lock()
        self.first_seen: dict[tuple[str, str], float] = {}   # (commitment, sig) -> monotonic
        self.aborted = ""

    def write(self, row: dict) -> None:
        self.fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.fh.flush()

    async def pace(self) -> None:
        """Process-wide floor between RPC calls. Bursting this key throttles the funded lane."""
        async with self.gate:
            wait = self.next_slot - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.next_slot = time.monotonic() + self.min_gap

    def _rpc(self, method: str, params: list, stats: dict):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.http_url, data=body,
                                     headers={"content-type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    result, err = decode_rpc(json.loads(r.read().decode()))
                stats["attempts"] = attempt + 1
                # A REJECTED CALL IS NOT AN EMPTY ANSWER. Reading only `result` made
                # `getTransaction` at `processed` — which the node refuses with -32602 — look
                # like "not readable yet", so it burned both retry sleeps and reported 22 of 22
                # nulls as if the chain were slow (Gamma, 2026-09-23). An error returns at once.
                if err is not None:
                    stats["rpc_error"] = err
                    return None
                # The watcher's null-retry: the socket fires before getTransaction can read the
                # transaction, so a null is "not readable yet", never "no such transaction".
                if result is None and method == "getTransaction" and attempt < 2:
                    stats["nulls"] = stats.get("nulls", 0) + 1
                    time.sleep(0.4)
                    stats["sleep_ms"] = stats.get("sleep_ms", 0.0) + 400.0
                    continue
                if result is None:
                    stats["nulls"] = stats.get("nulls", 0) + 1
                return result
            except Exception as exc:
                stats["attempts"] = attempt + 1
                if "429" in str(exc):
                    stats["http_429"] = stats.get("http_429", 0) + 1
                    self.n_429 += 1
                stats["error"] = repr(exc)[:120]
                if attempt == 2:
                    return None
                time.sleep(0.3)
                stats["sleep_ms"] = stats.get("sleep_ms", 0.0) + 300.0
        return None

    async def fetch(self, sig: str, owner: str, handle: str, commitment: str,
                    frame_mono: float) -> None:
        """getTransaction for a frame THIS socket saw first, with the watcher's null-retry.

        THE READ IS ALWAYS `confirmed`, on both sides. The node refuses anything below it
        (-32602, "Method does not support commitment below `confirmed`"), so a `processed` read
        is not a faster read, it is no read at all. The sockets keep their own commitments, and
        the question becomes whether the earlier FRAME starts the confirmed read sooner and
        reaches an actionable row sooner — which is the half that decides money.
        """
        stats: dict = {"attempts": 0, "nulls": 0, "sleep_ms": 0.0, "http_429": 0}
        t_queued = time.monotonic()
        await self.pace()
        t0 = time.monotonic()
        tx = await asyncio.get_event_loop().run_in_executor(
            None, self._rpc, "getTransaction",
            [sig, {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed",
                   "commitment": RPC_COMMITMENT}], stats)
        t_parsed = time.monotonic()
        actionable = None
        if tx:
            # A parsed transaction is not yet an actionable one: the lane acts on BUYS. Imported
            # lazily: importing the watcher reads .env and builds its key list, which a --selftest
            # must not need.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from onchain_wallet_watch import classify_buy
            try:
                actionable = classify_buy(tx, owner) is not None
            except Exception as exc:                       # a shape we cannot read is not a buy
                stats["classify_error"] = repr(exc)[:120]
                actionable = None
        if tx:
            outcome = "buy" if actionable else "parsed_not_buy"
        elif stats.get("rpc_error"):
            outcome = "rpc_error"
        else:
            outcome = "null"
        self.write({
            "event": "gettx", "ts": now_iso(), "commitment": commitment,
            "rpc_commitment": RPC_COMMITMENT, "sig": sig, "owner": owner, "handle": handle,
            "frame_to_parsed_ms": round((t_parsed - frame_mono) * 1000.0, 2),
            "pacer_wait_ms": round((t0 - t_queued) * 1000.0, 2),
            "rpc_ms": round((t_parsed - t0) * 1000.0, 2),
            "outcome": outcome, "actionable": bool(actionable), **stats})
        if self.n_429 >= self.max_429:
            self.aborted = f"{self.n_429} HTTP 429 on the watcher's key"

    async def socket(self, ws_url: str, commitment: str, addrs: dict[str, str],
                     deadline: float) -> None:
        """Hold one commitment's socket until the deadline, reconnecting on a drop.

        A drop is normal: Gamma's first run died at 15 min 56 s of 60 with `no close frame
        received or sent`, and with one shared gather that killed the other socket too. Each
        socket now owns its own reconnect, and a resubscribe starts a new first-seen window for
        THIS commitment only — the gap is recorded so a delta across it is not read as latency.
        """
        backoff = 1.0
        while time.monotonic() < deadline and not self.aborted:
            try:
                await self._session(ws_url, commitment, addrs, deadline)
                backoff = 1.0
            except Exception as exc:
                self.write({"event": "socket_drop", "ts": now_iso(), "commitment": commitment,
                            "error": repr(exc)[:200], "backoff_s": backoff})
                if time.monotonic() + backoff >= deadline:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _session(self, ws_url: str, commitment: str, addrs: dict[str, str],
                       deadline: float) -> None:
        import websockets
        async with websockets.connect(ws_url, max_size=8_000_000, ping_interval=20) as ws:
            sub_of: dict[int, str] = {}
            order = list(addrs)
            for i, addr in enumerate(order, start=1):
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                          "params": [{"mentions": [addr]},
                                                     {"commitment": commitment}]}))
            acked = 0
            while time.monotonic() < deadline and not self.aborted:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue
                mono, wall = time.monotonic(), now_iso()
                msg = json.loads(raw)
                if msg.get("id") is not None:
                    if "error" in msg:
                        self.write({"event": "sub_error", "ts": wall, "commitment": commitment,
                                    "id": msg["id"], "error": str(msg["error"])[:200]})
                    else:
                        acked += 1
                        sub_of[msg["result"]] = order[msg["id"] - 1]
                        if acked == len(order):
                            self.write({"event": "subscribed", "ts": wall,
                                        "commitment": commitment, "acked": acked})
                    continue
                if msg.get("method") != "logsNotification":
                    continue
                val = msg["params"]["result"]["value"]
                sig = val.get("signature")
                owner = sub_of.get(msg["params"]["subscription"], "")
                if not sig:
                    continue
                key = (commitment, sig)
                dup = key in self.first_seen
                if not dup:
                    self.first_seen[key] = mono
                self.write({
                    "event": "frame", "ts": wall, "commitment": commitment, "sig": sig,
                    "owner": owner, "handle": addrs.get(owner, ""), "mono": round(mono, 6),
                    "raw_len": len(raw), "err": bool(val.get("err")), "duplicate": dup,
                    "slot": msg["params"]["result"].get("context", {}).get("slot")})
                # One getTransaction per (socket, signature): the confirmed side is the control,
                # and without it the parse penalty has no baseline.
                if not dup and not val.get("err") and owner:
                    asyncio.create_task(
                        self.fetch(sig, owner, addrs.get(owner, ""), commitment, mono))


async def run(args) -> int:
    http_url, ws_url = endpoint(args.endpoint_env)
    addrs = owners(args.max_owners)
    if not addrs:
        raise SystemExit("no confirmed solana addresses in memecoin_wallet_addresses")
    if len(COMMITMENTS) > args.max_sockets:
        raise SystemExit(f"{len(COMMITMENTS)} sockets over --max-sockets {args.max_sockets}")
    probe = Probe(http_url, Path(args.out), args.max_rps, args.max_429)
    probe.write({"event": "start", "ts": now_iso(), "owners": len(addrs),
                 "sockets": len(COMMITMENTS), "duration_s": args.duration,
                 "endpoint_env": args.endpoint_env, "max_rps": args.max_rps})
    print(f"{len(addrs)} owners x {len(COMMITMENTS)} sockets, {args.duration}s -> {args.out}",
          flush=True)
    deadline = time.monotonic() + args.duration
    fatal = None
    try:
        # return_exceptions=True: one socket's death must not end the other's capture, and a
        # crash must still reach the `stop` row below — a run with no summary has to be
        # reconstructed from the first and last JSONL lines.
        results = await asyncio.gather(
            *[probe.socket(ws_url, c, addrs, deadline) for c in COMMITMENTS],
            return_exceptions=True)
        fatal = [repr(r)[:200] for r in results if isinstance(r, BaseException)] or None
    except BaseException as exc:                       # KeyboardInterrupt included, on purpose
        fatal = [repr(exc)[:200]]
        raise
    finally:
        probe.write({"event": "stop", "ts": now_iso(), "aborted": probe.aborted,
                     "frames_first_seen": len(probe.first_seen), "http_429": probe.n_429,
                     "fatal": fatal})
        probe.fh.close()
    if fatal:
        print(f"socket task(s) ended with: {fatal}", flush=True)
    if probe.aborted:
        print(f"ABORTED: {probe.aborted}", flush=True)
    return 0


def selftest_rpc_error() -> None:
    """A rejected call is an error with a code, never a null, and it must not retry."""
    result, err = decode_rpc({"jsonrpc": "2.0", "id": 1, "error": {
        "code": -32602,
        "message": "Invalid params: Method does not support commitment below `confirmed`"}})
    assert result is None, result
    assert err and err["code"] == -32602, err
    stats: dict = {"attempts": 0, "nulls": 0}
    if err is not None:                                # the branch _rpc takes: return, no retry
        stats["rpc_error"] = err
        stats["attempts"] = 1
    assert stats["attempts"] == 1, stats               # one call, both retry sleeps skipped
    assert stats["nulls"] == 0, stats                  # a rejection never counts as a null
    ok_result, ok_err = decode_rpc({"result": {"meta": {}}, "id": 1})
    assert ok_err is None and ok_result == {"meta": {}}, (ok_result, ok_err)
    null_result, null_err = decode_rpc({"result": None, "id": 1})
    assert null_result is None and null_err is None, (null_result, null_err)
    print("selftest ok: -32602 reads as rpc_error with 1 attempt, 0 nulls, 0 retries")


def selftest() -> int:
    """One synthetic frame pair, and the delta the analysis will compute from the JSONL."""
    selftest_rpc_error()
    rows = [
        {"event": "frame", "commitment": "processed", "sig": "SIG", "mono": 100.000},
        {"event": "frame", "commitment": "confirmed", "sig": "SIG", "mono": 100.530},
        {"event": "frame", "commitment": "processed", "sig": "SIG", "mono": 100.900,
         "duplicate": True},
    ]
    first: dict[tuple[str, str], float] = {}
    for r in rows:
        first.setdefault((r["commitment"], r["sig"]), r["mono"])
    delta_ms = (first[("confirmed", "SIG")] - first[("processed", "SIG")]) * 1000.0
    assert round(delta_ms, 1) == 530.0, delta_ms
    assert first[("processed", "SIG")] == 100.000, "a duplicate frame must not move first-seen"
    only_processed = {s for (c, s) in first if c == "processed"} - {
        s for (c, s) in first if c == "confirmed"}
    assert only_processed == set(), only_processed
    print(f"selftest ok: processed leads confirmed by {delta_ms:.0f} ms on the synthetic pair")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--duration", type=float, default=3600.0)
    ap.add_argument("--max-sockets", type=int, default=40,
                    help="ceiling on concurrent WebSockets; this probe opens one per commitment")
    ap.add_argument("--max-owners", type=int, default=None)
    ap.add_argument("--max-rps", type=float, default=5.0,
                    help="process-wide getTransaction rate floor; this key feeds the funded lane")
    ap.add_argument("--max-429", type=int, default=5, help="abort after this many HTTP 429s")
    ap.add_argument("--endpoint-env", default="ONCHAIN_WATCH_HELIUS_RPC_URL")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Does a TOPIC-ONLY V4 Swap subscription stall when the ADDRESS-FILTERED one does?

WHY (2026-09-23). publicnode let the live writer's address-filtered V4 subscription drift from 0.7 s
to 652.9 s of delivery lag over fourteen minutes and then drop 2,754 blocks (70651082..70653834,
16:19:39Z-16:24:14Z), while the TOPIC-ONLY V3 subscription on the SAME socket stayed at 0.6-1.4 s
throughout and `newHeads` never missed a beat. The two subscriptions differ in exactly one way: one
carries an `address` filter and the other does not. If the filter is what stalls, moving V4 to a
topic-only subscription is a one-line change; if a topic-only V4 subscription stalls too, it is the
provider's V4 traffic and the fix is elsewhere.

WHAT THIS DOES. Opens ONE socket to the tape's own provider, subscribes to `newHeads` plus V4 Swap
BY TOPIC ONLY (no address filter), and records one row per frame: block number, arrival time, and
the header timestamp once it is known. Nothing else. It writes a JSONL file and touches no
production table.

⚠️ THIS IS A SECOND SOCKET TO THE PROVIDER THAT FEEDS THE FUNDED m5 GATE. Heavy publicnode reads
from this IP have stalled the live tape before (2026-09-22), which is the failure this whole
investigation is about. So: ONE socket, no backfill, no `eth_getLogs`, no burst, and a duration the
operator sets. A passive subscription is the lightest possible instrument for this question, and it
is still not free.

READING IT. The comparison is against the live tape's own rows for the same minutes
(`data/memecoin_onchain_volume.db`, 66-character pool ids = V4), which the writer is producing
anyway. Per-minute median delivery lag on both sides, and block continuity on both sides:

    python3 scripts/research/memecoin_v4_topic_vs_address_lag.py --capture --seconds 3600
    python3 scripts/research/memecoin_v4_topic_vs_address_lag.py --report

⚠️ THE TOPIC-ONLY SUBSCRIPTION SEES EVERY V4 POOL ON THE CHAIN, not just this manager's, so its
frame count is NOT comparable to the tape's row count. Only the LAG and the CONTINUITY of blocks are
comparable, and the report says so rather than printing a ratio nobody can use.
"""
import argparse
import asyncio
import contextlib
import datetime as dt
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files            # noqa: E402
from sniper.util.state_paths import state_root, state_path  # noqa: E402

load_env_files(str(state_root()))

import sniper.settings.memecoin as cfg                     # noqa: E402
from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, V4_SWAP  # noqa: E402

OUT_PATH = "data/v4_topic_vs_address.jsonl"
CHAIN = "4663"


def _now():
    return dt.datetime.now(dt.timezone.utc)


async def capture(seconds, out_path, url):
    """One socket at a time: newHeads plus V4 Swap by topic only, reconnecting until the deadline.

    ⚠️ A DROPPED SOCKET IS NOT THE STALL THIS PROBE IS LOOKING FOR, and the two must not be
    conflated. The stall class is a subscription falling minutes behind while the connection stays
    UP; a `ConnectionClosedError` or a keepalive ping timeout is the reconnect regime, which the
    live writer already handles by reconnecting and backfilling. The first run of this probe died
    that way after 14 minutes, so the loop exists to record an hour, not to paper over a drop: every
    reconnect writes its own row and the report prints them.
    """
    import websockets

    started = _now().timestamp()
    counts = {"heads": 0, "swaps": 0, "acked": 0, "errors": 0, "reconnects": 0}
    with open(out_path, "a") as handle:
        handle.write(json.dumps({"kind": "start", "at": _now().isoformat(),
                                 "seconds": seconds, "url": url,
                                 "manager": V4_POOL_MANAGERS.get(CHAIN)}) + "\n")
        while _now().timestamp() - started < seconds:
            try:
                await _one_socket(ws_url=url, deadline=started + seconds, handle=handle,
                                  counts=counts)
            except Exception as exc:
                counts["reconnects"] += 1
                handle.write(json.dumps({"kind": "socket_drop", "at": _now().isoformat(),
                                         "error": "%s: %s" % (type(exc).__name__, str(exc)[:160]),
                                         "elapsed_s": round(_now().timestamp() - started, 1)}) + "\n")
                handle.flush()
                if _now().timestamp() - started >= seconds:
                    break
                await asyncio.sleep(2.0)     # one socket at a time, and never a reconnect storm
        handle.write(json.dumps({"kind": "stop", "at": _now().isoformat(), **counts}) + "\n")
    print("capture done:", counts)
    return 0


async def _one_socket(*, ws_url, deadline, handle, counts):
    """One connection, until the deadline or until it drops. Raises on a drop; the caller records it."""
    import websockets

    async with websockets.connect(ws_url, open_timeout=15, max_size=None,
                                  ping_interval=20) as ws:
        # NO `address` KEY. That single difference is the whole question.
        for rid, params in ((2, ["newHeads"]), (3, ["logs", {"topics": [[V4_SWAP]]}])):
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid,
                                      "method": "eth_subscribe", "params": params}))
        subs = {}
        while _now().timestamp() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                handle.write(json.dumps({"kind": "recv_timeout",
                                         "at": _now().isoformat()}) + "\n")
                handle.flush()
                continue
            at = _now().timestamp()
            try:
                msg = json.loads(raw)
            except ValueError:
                counts["errors"] += 1
                continue
            if msg.get("error"):
                counts["errors"] += 1
                handle.write(json.dumps({"kind": "error", "at": _now().isoformat(),
                                         "error": str(msg["error"])[:200]}) + "\n")
                continue
            if msg.get("id") in (2, 3) and msg.get("result"):
                subs[msg["result"]] = msg["id"]
                counts["acked"] += 1
                continue
            params = msg.get("params") or {}
            which = subs.get(params.get("subscription"))
            item = params.get("result") or {}
            if which == 2:
                counts["heads"] += 1
                handle.write(json.dumps({
                    "kind": "head", "at": at,
                    "block": int(item["number"], 16) if item.get("number") else None,
                    "block_ts": int(item["timestamp"], 16) if item.get("timestamp") else None,
                }) + "\n")
            elif which == 3:
                counts["swaps"] += 1
                handle.write(json.dumps({
                    "kind": "swap", "at": at,
                    "block": int(item["blockNumber"], 16) if item.get("blockNumber") else None,
                    "address": str(item.get("address") or "").lower(),
                    "tx": item.get("transactionHash"),
                }) + "\n")
            if counts["swaps"] % 200 == 0:
                handle.flush()


def _rows(path):
    heads, swaps, stops = {}, [], []
    with open(path) as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "head" and row.get("block") and row.get("block_ts"):
                heads[int(row["block"])] = float(row["block_ts"])
            elif row.get("kind") == "swap" and row.get("block"):
                swaps.append(row)
            elif row.get("kind") in ("stop", "error", "recv_timeout", "socket_drop"):
                # ⚠️ `socket_drop` WAS WRITTEN AND NEVER READ (N14). The capture records every
                # reconnect, and the report dropped them on the floor — so a run whose socket died
                # and recovered read as a clean hour, which is the single most important thing this
                # probe exists to detect.
                stops.append(row)
    return heads, swaps, stops


def _per_minute(pairs):
    """{minute: [lags]} from (arrival_epoch, lag) pairs."""
    out = {}
    for at, lag in pairs:
        # THE DATE IS PART OF THE KEY (N15). "%H:%M" collides across days, so an hour spanning
        # midnight — or two runs of the same file — merge 00:05 into 00:05 and average two
        # different minutes together.
        minute = dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")
        out.setdefault(minute, []).append(lag)
    return out


def report(path):
    heads, swaps, stops = _rows(path)
    if not swaps:
        print("no V4 frames captured; nothing to compare")
        return 1
    manager = str(V4_POOL_MANAGERS.get(CHAIN) or "").lower()
    topic_pairs, mine = [], []
    for row in swaps:
        stamp = heads.get(int(row["block"]))
        if stamp is None:
            continue                      # header not seen: no sample rather than a guessed one
        topic_pairs.append((float(row["at"]), float(row["at"]) - stamp))
        if str(row.get("address") or "") == manager:
            mine.append((float(row["at"]), float(row["at"]) - stamp))

    if not topic_pairs:
        # min() on an empty sequence raises, and an empty topic side is a real outcome: a capture
        # that recorded only heads has nothing to compare and must say so rather than crash.
        print("no topic-only V4 frames in the capture; nothing to compare")
        return 1
    lo = min(at for at, _ in topic_pairs)
    # AND THE CAPTURE'S END BOUNDS THE LIVE SIDE (N15). Without an upper bound the tape query keeps
    # reading rows that arrived AFTER the probe stopped, so the live arm covers a longer window than
    # the topic arm and the two medians are taken over different spans.
    hi = max(at for at, _ in topic_pairs)
    print("TOPIC-ONLY V4 SUBSCRIPTION (this capture)")
    print("  window %s .. %s" % (dt.datetime.fromtimestamp(lo, dt.timezone.utc).isoformat(),
                                 dt.datetime.fromtimestamp(hi, dt.timezone.utc).isoformat()))
    print("  frames with a known header: %d of %d, of which %d are this manager's pools"
          % (len(topic_pairs), len(swaps), len(mine)))
    print("  ⚠️ a topic-only subscription sees EVERY V4 pool on the chain, so its frame count is")
    print("     not comparable to the tape's row count. Only lag and continuity are.")

    per = _per_minute(topic_pairs)
    over = [m for m, v in per.items() if statistics.median(v) > 30.0]
    print("  per-minute median lag: minutes=%d  p50=%.2fs  worst=%.2fs  minutes over 30s=%d"
          % (len(per),
             statistics.median([statistics.median(v) for v in per.values()]),
             max(statistics.median(v) for v in per.values()), len(over)))
    if over:
        print("  stalled minutes:", ", ".join(sorted(over)))

    # The live address-filtered side, from the tape the writer is producing anyway.
    tape = sqlite3.connect("file:%s?mode=ro" % state_path("data/memecoin_onchain_volume.db"),
                           uri=True)
    tape.row_factory = sqlite3.Row
    with contextlib.closing(tape):
        rows = tape.execute(
            "SELECT t.observed_at, b.block_ts FROM pool_ticks t "
            "JOIN block_times b ON b.chain_id=t.chain_id AND b.block_no=t.block_no "
            "WHERE t.chain_id=? AND length(t.pool)=66 AND t.observed_at>=? AND t.observed_at<=?",
            (CHAIN, dt.datetime.fromtimestamp(lo, dt.timezone.utc).isoformat(),
             dt.datetime.fromtimestamp(hi, dt.timezone.utc).isoformat())).fetchall()
    live = []
    for row in rows:
        try:
            arrived = dt.datetime.fromisoformat(str(row["observed_at"])).timestamp()
            stamp = dt.datetime.fromisoformat(str(row["block_ts"]).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            continue
        live.append((arrived, arrived - stamp))
    print("LIVE ADDRESS-FILTERED SUBSCRIPTION (the writer's own rows, same minutes)")
    if not live:
        print("  no rows in the tape for this window (retention is about an hour)")
    else:
        per_live = _per_minute(live)
        over_live = [m for m, v in per_live.items() if statistics.median(v) > 30.0]
        print("  per-minute median lag: minutes=%d  p50=%.2fs  worst=%.2fs  minutes over 30s=%d"
              % (len(per_live),
                 statistics.median([statistics.median(v) for v in per_live.values()]),
                 max(statistics.median(v) for v in per_live.values()), len(over_live)))
        if over_live:
            print("  stalled minutes:", ", ".join(sorted(over_live)))
        both = sorted(set(per) & set(per_live))
        print("  minutes present on BOTH sides: %d" % len(both))
        for minute in both:
            a = statistics.median(per[minute])
            b = statistics.median(per_live[minute])
            if a > 30.0 or b > 30.0:
                print("    %s  topic %.1fs  address %.1fs" % (minute, a, b))

    # Block continuity on the topic-only side: a gap wider than this is what the live side showed.
    blocks = sorted({int(r["block"]) for r in swaps})
    gaps = [(lo_b, hi_b) for lo_b, hi_b in zip(blocks, blocks[1:]) if hi_b - lo_b > 50]
    print("BLOCK CONTINUITY (topic-only): %d distinct blocks, %d gaps wider than 50 blocks"
          % (len(blocks), len(gaps)))
    for lo_b, hi_b in gaps[:10]:
        print("    %d -> %d (%d blocks)" % (lo_b, hi_b, hi_b - lo_b))
    if gaps and stops:
        # ⚠️ A GAP IS NOT A CHAIN GAP WHEN THE CAPTURE ITSELF STOPPED. The output file is APPENDED
        # across runs, so a report over a file holding two captures shows the span between them as
        # one enormous "gap" — 63,944 blocks on the 2026-09-23 file, which is the 17:43 stop and
        # the 19:30 restart, not a missed block. Reading the stop events at all is N14's fix; this
        # is what they are for. Split the file per run before reading a gap as evidence.
        print("    ⚠️ %d stop/drop event(s) below: a gap spanning one is a CAPTURE boundary, not a"
              " chain gap, and this file is appended across runs" % len(stops))
    for row in stops:
        print("EVENT:", json.dumps(row)[:200])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--seconds", type=float, default=3600.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--url", default=None)
    a = ap.parse_args(argv)
    out_path = a.out or str(state_path(OUT_PATH))
    if a.capture:
        url = a.url or cfg.MEMECOIN_ROBINHOOD_VOLUME_WS_URL
        return asyncio.run(capture(a.seconds, out_path, url))
    if a.report:
        return report(out_path)
    ap.error("choose --capture or --report")


if __name__ == "__main__":
    raise SystemExit(main())

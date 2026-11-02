#!/usr/bin/env python3
"""Read-only: for each TPU sidecar send, which leg acked first and how many slots it took to land.

The submission ledger records which leg answered FIRST (an RPC's HTTP ack or the TPU's write to a
leader). That is not the question: the same signed bytes go out on every leg, and what matters is
when the transaction lands. This joins, per transaction:
  - the sidecar's send line (logs/memecoin_tpu_sidecar.log): send slot (`slot=`, lines from
    3921b7b7 on), connections opened/reused, send and per-leader ms;
  - the ledger's first accepted leg (memecoin_submission_attempts);
  - the landing slot from getTransaction at `confirmed` on the FIRST entry of
    MEMECOIN_RESEARCH_HELIUS_RPC_URLS, calls >= 1.1 s apart. Never the funded custody RPC.
and prints one row per send plus land-minus-send by first-acked leg.

    python3 scripts/research/tpu_landing_check.py --since 2026-09-27T01:31
    python3 scripts/research/tpu_landing_check.py --selftest
"""
import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request

REPO = "/home/bigpapi/Desktop/Sniper"
LOG = os.path.join(REPO, "logs", "memecoin_tpu_sidecar.log")
DB = os.path.join(REPO, "sniper_tracker.db")
LINE = re.compile(r"^memecoin-tpu-sidecar send (?P<fields>.*)$")


def parse_send_line(line):
    """{field: value} from one send line, or None. Values stay strings; `slot` may be absent."""
    m = LINE.match(line.strip())
    if not m:
        return None
    out = dict(kv.split("=", 1) for kv in m.group("fields").split() if "=" in kv)
    return out if out.get("tx") else None


def first_accepted(conn, tx):
    row = conn.execute(
        "SELECT provider_id, latency_ms FROM memecoin_submission_attempts "
        "WHERE tx_identity = ? AND state = 'accepted' ORDER BY latency_ms LIMIT 1", (tx,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="UTC ISO; only send lines at/after this unix_ms equivalent")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    sys.path.insert(0, REPO)
    from datetime import datetime, timezone
    from sniper.services.memecoin_research_rpc import funded_overlap, research_urls
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    urls = research_urls()
    if not urls or funded_overlap(urls[:1]):
        raise SystemExit("no research key, or it overlaps a funded key: refusing")
    url = urls[0]
    since_ms = 0
    if args.since:
        since_ms = int(datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() * 1000)
    with open(LOG, errors="replace") as fh:
        sends = [s for s in map(parse_send_line, fh) if s and int(s.get("unix_ms", 0)) >= since_ms]
    last = [0.0]

    def rpc(method, params):
        wait = 1.1 - (time.monotonic() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.monotonic()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, body, {"content-type": "application/json",
                                                 "user-agent": "Mozilla/5.0 Sniper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return {"_http": exc.code}
        return out.get("result") if "error" not in out else {"_error": out["error"]}

    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    rows = []
    for s in sends:
        leg, ack_ms = first_accepted(conn, s["tx"])
        tx = rpc("getTransaction", [s["tx"], {"commitment": "confirmed", "encoding": "base64",
                                              "maxSupportedTransactionVersion": 1}])
        # A failed read is not "did not land": name what came back (a 429 read as null once).
        fetch = ("http_%s" % tx["_http"] if isinstance(tx, dict) and "_http" in tx
                 else "rpc_error" if isinstance(tx, dict) and "_error" in tx
                 else "null" if tx is None else "ok")
        landed = tx.get("slot") if isinstance(tx, dict) else None
        err = ((tx.get("meta") or {}).get("err") if isinstance(tx, dict) and "slot" in tx else None)
        send_slot = int(s["slot"]) if s.get("slot", "").isdigit() else None
        rows.append({"tx": s["tx"][:8], "first_ack": (leg or "-").split(":")[-1],
                     "ack_ms": ack_ms, "tpu_send_ms": float(s.get("send_ms", "nan")),
                     "new": s.get("new_connections"), "reused": s.get("reused"),
                     "leader_ms": s.get("leader_ms", "-"), "send_slot": send_slot,
                     "landed": landed, "land_minus_send": (landed - send_slot)
                     if landed and send_slot else None, "err": err, "fetch": fetch})
    conn.close()
    for r in rows:
        print(json.dumps(r, default=str))
    by_leg = {}
    for r in rows:
        if r["land_minus_send"] is not None:
            by_leg.setdefault(r["first_ack"], []).append(r["land_minus_send"])
    for leg, vals in sorted(by_leg.items()):
        print("first_ack=%s n=%d land-send slots median=%s min=%s max=%s" % (
            leg, len(vals), statistics.median(vals), min(vals), max(vals)))
    print("sends=%d with_send_slot=%d landed=%d" % (
        len(rows), sum(r["send_slot"] is not None for r in rows),
        sum(r["landed"] is not None for r in rows)))


def selftest():
    line = ("memecoin-tpu-sidecar send outcome=accepted tx=Sig111 slot=450860000 leaders=2 "
            "new_connections=2 reused=0 connection_errors=0 zero_rtt=0/0 make_connection_ms=61 "
            "send_ms=41.2 leader_ms=41.0,22.0 leader_ok=1,0 counters=delta inflight=1 unix_ms=5")
    got = parse_send_line(line)
    assert got["tx"] == "Sig111" and got["slot"] == "450860000" and got["leader_ms"] == "41.0,22.0"
    old = parse_send_line("memecoin-tpu-sidecar send outcome=accepted tx=Sig2 leaders=2 unix_ms=1")
    assert old["tx"] == "Sig2" and "slot" not in old
    assert parse_send_line("memecoin-tpu-sidecar ready fanout_slots=5") is None
    print("selftest ok")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Replay the 4663 route ladder's OWN recorded eth_calls against three hosts and compare RTT.

Read-only against the chain. Payloads come from `memecoin_entry_attempts.trace_json`, written by
the ladder payload tap (first live in release b3c6438e, runner start 2026-09-24T16:17:42Z), so this
measures the real thing rather than a proxy call.

Rules this obeys, each one load-bearing:
  * `.env` is loaded through `sniper.util.envfiles.load_env_files`. A settings read in a bare
    process silently returns the CODE DEFAULT, which is narrower than production.
  * Stops before the next request if a funded 4663 position opens.
  * >=1 s spacing per host and a hard cap on calls per host.
  * publicnode 4663 refuses Python's default User-Agent with HTTP 403 and no explanatory body, so a
    User-Agent is always sent and the one used is reported per host.
  * The JSON-RPC `error` field is read explicitly. A client that reads only `result` sees a
    rejection as a null.
  * Identity first: the QuickNode host must answer `eth_chainId` with 0x1237 (4663) before any
    payload is sent, or every later number would describe the wrong chain.

Usage:
    python3 scripts/research/evm_rpc_rtt_replay.py [--ladders N] [--repeats N] [--cap N]
    python3 scripts/research/evm_rpc_rtt_replay.py --selftest
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
import urllib.request

REPO = "/home/bigpapi/Desktop/Sniper"
sys.path.insert(0, REPO)
TAP_START = "2026-09-24T16:17:42+00:00"
UA_CURL = "curl/8.5.0"
UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120 Safari/537.36")


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * q))] if v else None


def extract_payloads(trace_rows, limit_ladders):
    """Distinct (method, params) the ladder actually sent, from the newest ladders first.

    Pure function over trace JSON strings so the extraction can be tested without a database.
    Returns (payloads, ladders_used).
    """
    seen, out, ladders = set(), [], 0
    for text in trace_rows:
        if not text or '"calls"' not in text:
            continue
        try:
            tr = json.loads(text)
        except Exception:
            continue
        used = False
        for e in tr.get("substage_events") or []:
            if not str(e.get("name")).startswith("evm.quote"):
                continue
            for rung in e.get("rungs") or []:
                for call in rung.get("calls") or []:
                    m, p = call.get("method"), call.get("params")
                    if not m:
                        continue
                    key = json.dumps([m, p], sort_keys=True)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"method": m, "params": p, "rung": rung.get("rung")})
                    used = True
        if used:
            ladders += 1
            if ladders >= limit_ladders:
                break
    return out, ladders


def selftest():
    good = json.dumps({"substage_events": [{"name": "evm.quote_initial", "rungs": [
        {"rung": "v4", "calls": [{"method": "eth_call", "params": [{"to": "0x1"}, "latest"]},
                                 {"method": "eth_call", "params": [{"to": "0x1"}, "latest"]}]},
        {"rung": "v2", "calls": [{"method": "eth_call", "params": [{"to": "0x2"}, "latest"]}]}]}]})
    untapped = json.dumps({"substage_events": [{"name": "evm.quote_initial",
                                                "rungs": [{"rung": "v4", "hit": True}]}]})
    pl, n = extract_payloads([untapped, good], 3)
    assert n == 1, "an untapped ladder must not count as a tapped one: %r" % n
    assert len(pl) == 2, "duplicate payloads must collapse to one: %r" % len(pl)
    assert {p["rung"] for p in pl} == {"v4", "v2"}
    pl, n = extract_payloads([untapped], 3)
    assert (pl, n) == ([], 0), "no calls[] means nothing to replay"
    print("selftest ok: dedup, untapped rows skipped, rung kept")


def rpc(url, method, params, ua):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params if params is not None else []}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": ua})
    t0 = time.monotonic()
    try:
        raw = urllib.request.urlopen(req, timeout=20).read()
    except Exception as exc:
        return (time.monotonic() - t0) * 1000.0, "transport:%s" % type(exc).__name__, str(exc)[:80], None
    ms = (time.monotonic() - t0) * 1000.0
    try:
        j = json.loads(raw)
    except Exception:
        return ms, "decode", str(raw[:60]), None
    if isinstance(j, dict) and j.get("error"):
        return ms, "rpc_error", str(j["error"])[:90], None
    return ms, None, None, (j or {}).get("result")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ladders", type=int, default=3)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--cap", type=int, default=100)
    ap.add_argument("--spacing", type=float, default=1.0)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if not math.isfinite(a.spacing) or a.spacing < 1.0:
        ap.error("--spacing must be finite and at least 1 second")
    if a.selftest:
        selftest()
        return 0

    from sniper.util.envfiles import load_env_files
    load_env_files(REPO)
    qn = os.getenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663")
    if not qn:
        print("ABORT: MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663 is not set")
        return 1
    hosts = [("chain_owner", "https://rpc.mainnet.chain.robinhood.com", UA_BROWSER),
             ("publicnode", "https://robinhood-rpc.publicnode.com", UA_CURL),
             ("quicknode", qn, UA_BROWSER)]
    conn = sqlite3.connect("file:%s/sniper_tracker.db?mode=ro" % REPO, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row

    def funded_open():
        return conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                            "AND is_open=1 AND network_id='4663'").fetchone()[0]

    if funded_open():
        print("ABORT: funded 4663 position open")
        return 1
    ms, err, detail, result = rpc(qn, "eth_chainId", [], UA_BROWSER)
    print("QuickNode identity: eth_chainId -> %s (%.0f ms)" % (result if not err else err, ms))
    if err or not result or int(result, 16) != 4663:
        print("ABORT: identity check failed (%s %s)" % (err, detail))
        return 1

    texts = [r["trace_json"] for r in conn.execute(
        "SELECT trace_json FROM memecoin_entry_attempts WHERE network_id='4663' "
        " AND decision_started_at>? ORDER BY decision_started_at DESC", (TAP_START,))]
    pl, ladders = extract_payloads(texts, a.ladders)
    print("distinct payloads: %d from %d tapped ladders" % (len(pl), ladders))
    if not pl:
        print("ABORT: no tapped payloads")
        return 1
    rounds = max(1, min(a.repeats, a.cap // max(1, len(pl))))
    print("plan: %d rounds x %d payloads per host, cap %d, >=%.1f s spacing\n"
          % (rounds, len(pl), a.cap, a.spacing))

    res = {h[0]: {"ms": {}, "err": {}, "n": 0} for h in hosts}
    calls_by_method = {}
    stopped = None
    for rnd in range(rounds):
        if funded_open():
            stopped = "funded 4663 position opened in round %d" % rnd
            break
        for item in pl:
            for name, url, ua in hosts:
                if res[name]["n"] >= a.cap:
                    continue
                if funded_open():
                    stopped = "funded 4663 position opened in round %d" % rnd
                    break
                ms, err, detail, _ = rpc(url, item["method"], item["params"], ua)
                res[name]["n"] += 1
                if name == "quicknode":
                    calls_by_method[item["method"]] = calls_by_method.get(item["method"], 0) + 1
                if err:
                    k = "%s | %s" % (err, (detail or "")[:48])
                    res[name]["err"][k] = res[name]["err"].get(k, 0) + 1
                else:
                    res[name]["ms"].setdefault(item["method"], []).append(ms)
            if stopped:
                break
            time.sleep(a.spacing)
        if stopped:
            break
    print("RESULTS%s\n" % ("" if not stopped else "  (STOPPED: %s)" % stopped))
    for name, url, ua in hosts:
        d = res[name]
        allms = [x for v in d["ms"].values() for x in v]
        print("  %-12s calls=%3d ok=%3d errors=%3d UA=%s"
              % (name, d["n"], len(allms), sum(d["err"].values()), ua.split("/")[0]))
        if allms:
            print("      all methods        p50 %7.1f ms  p90 %7.1f ms  min %6.1f  max %7.1f"
                  % (pct(allms, .5), pct(allms, .9), min(allms), max(allms)))
        for m, v in sorted(d["ms"].items(), key=lambda kv: -len(kv[1])):
            print("      %-18s n=%3d  p50 %7.1f  p90 %7.1f" % (m, len(v), pct(v, .5), pct(v, .9)))
        for k, n in sorted(d["err"].items(), key=lambda kv: -kv[1]):
            print("      ERROR x%-3d %s" % (n, k))
    print("\n  QuickNode calls issued by method (credit weight is per method; read the conversion")
    print("  off the provider dashboard, never assume it):")
    for m, n in sorted(calls_by_method.items(), key=lambda kv: -kv[1]):
        print("      %-18s %d" % (m, n))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

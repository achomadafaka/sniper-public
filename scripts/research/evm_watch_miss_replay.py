"""Why did the 4663 EVM watcher miss a J7 buy by a wallet it watches? One cause per transaction.

Gamma's fast-lane census (docs/audits/2026-09-28-fast-lane-coverage-census.md, build 2): since the
4663 watcher went live at 06:37Z it detected 3 of 18 J7 buys by wallets whose address it holds. This
replays each miss and names the cause, in this order:

  no_tx_hash        the J7 origin carries no transaction hash (a feed UUID); nothing to replay
  restart_gap       the watcher was stopped, or launched under STARTUP_S before the buy (watchdog log)
  not_watched       the J7 trader is not in the watcher's address set (armed wallet x verified book)
  no_transfer       the receipt carries no Transfer to the watched address, so the log subscription
                    (Transfer, topic2 = watched) could not have delivered it
  decoder:<why>     today's decode of the transaction does not find the buy
  arrival:<outcome> the stamped watcher log shows the transfer ARRIVED and was decided <outcome>,
                    although today's decode finds the buy (a read-at-arrival difference)
  silent_pre_fix    delivered-or-not unknown: before b47079c0 (11:08:05Z) a null tx/receipt read or a
                    socket stall dropped a transfer with no line, and today's decode finds the buy

Reads the ledger read-only, logs/memecoin_watchdog.log and logs/onchain_evm_armed.log. Chain reads go
to robinhood-rpc.publicnode.com only (curl UA, one call every SPACING_S; it answers a bare 403
after about 30 research calls in 5 minutes, and the watcher reads 4663 there too). The Relay code
check is seeded as verified (pins checked at go-live) because publicnode refuses eth_getCode; a
relay miss here is therefore a decode verdict, never an RPC one.

    python3 scripts/research/evm_watch_miss_replay.py [--since 2026-09-28T06:37:30] [--dry]
    python3 scripts/research/evm_watch_miss_replay.py --selftest
"""
import argparse
import contextlib
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

URL = "https://robinhood-rpc.publicnode.com"
SPACING_S = 10.0
STARTUP_S = 10.0
FIX_AT = "2026-09-28T11:08:05+00:00"       # b47079c0: stamped outcome line per delivered transfer
WATCHDOG_LOG = REPO / "logs" / "memecoin_watchdog.log"
WATCHER_LOG = REPO / "logs" / "onchain_evm_armed.log"
LAUNCH = re.compile(r"^(\S+Z) launched EVM on-chain watcher chain=4663 ")
STOP = re.compile(r"^(\S+Z) on-chain watcher chain=4663 (is from a previous release|unhealthy)")
OUTCOME = re.compile(r"^(\S+) \[onchain-evm\] transfer (0x[0-9a-f]{10})[0-9a-f]{0,54} arrival=\S+ outcome=(\S+)")
# The watcher prints the FULL tx hash since 6ee9b54f0 (2026-10-17); the key stays its 12-char prefix,
# which every lookup here uses (tx[:12]), and both line forms parse.
DETECTION = re.compile(r"\[onchain-evm\] detection (0x[0-9a-f]{10})[0-9a-f]{0,54} ")


def utc(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def watcher_windows(lines):
    """[(live_from, stopped_at or None)] for the 4663 watcher, from the watchdog's own lines."""
    windows = []
    for line in lines:
        m = LAUNCH.match(line)
        if m:
            windows.append([utc(m.group(1)) + timedelta(seconds=STARTUP_S), None])
            continue
        m = STOP.match(line)
        if m and windows and windows[-1][1] is None:
            windows[-1][1] = utc(m.group(1))
    return [tuple(w) for w in windows]


def up_at(windows, t):
    return any(a <= t and (b is None or t < b) for a, b in windows)


def arrival_outcomes(lines):
    out = {}
    for line in lines:
        m = OUTCOME.match(line)
        if m:
            out.setdefault(m.group(2), []).append(m.group(3))
        m = DETECTION.search(line)
        if m:
            out.setdefault(m.group(1), []).append("detected")
    return out


def classify(item, *, windows, arrivals, watched, receipt_to_watched, decoded, why):
    """One cause. `receipt_to_watched`/`decoded` are None when the chain was not read."""
    if not item["tx"]:
        return "no_tx_hash"
    if not up_at(windows, item["t0"]):
        return "restart_gap"
    if item["trader"] not in watched:
        return "not_watched"
    if receipt_to_watched is False:
        return "no_transfer"
    if decoded is False:
        return "decoder:%s" % (why or "no_buy")
    seen = arrivals.get(item["tx"][:12])
    if seen:
        return "arrival:%s" % ",".join(seen)
    if item["t0"] < utc(FIX_AT):
        return "silent_pre_fix"
    return "unexplained"


class Chain:
    def __init__(self):
        self.last, self.calls = 0.0, 0

    def __call__(self, method, params):
        wait = SPACING_S - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(URL, body, {"Content-Type": "application/json",
                                                 "User-Agent": "curl/8.5.0"})
        try:
            out = json.loads(urllib.request.urlopen(req, timeout=20).read())
        finally:
            self.last, self.calls = time.monotonic(), self.calls + 1
        if out.get("error"):
            raise RuntimeError("%s: %s" % (method, out["error"]))
        return out["result"]


def misses(conn, since, watched):
    """J7 4663 attempts by a watched trader with no watcher detection on the same tx."""
    items = []
    for r in conn.execute(
            "SELECT decision_started_at, source_handle, mint, origin_event_id FROM memecoin_entry_attempts "
            "WHERE network_id='4663' AND signal_source='follow_activity' AND decision_started_at>=? "
            "ORDER BY decision_started_at", (since,)):
        parts = str(r[3] or "").split(":")
        raybot = parts[:2] == ["swap", "raybot"] and len(parts) >= 5
        tx, trader = (parts[2].lower(), parts[3].lower()) if raybot else (None, None)
        if trader is not None and trader not in watched:
            continue          # the census counts only wallets the watcher holds
        if tx and conn.execute("SELECT 1 FROM onchain_wallet_detections WHERE network_id='4663' AND "
                               "LOWER(signature) LIKE ? LIMIT 1", ("%" + tx + "%",)).fetchone():
            continue
        items.append({"t0": utc(r[0]), "handle": r[1], "mint": str(r[2]).lower(), "tx": tx,
                      "trader": trader, "origin": r[3]})
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-28T06:37:30")
    ap.add_argument("--dry", action="store_true", help="classify without chain reads")
    a = ap.parse_args()
    import onchain_evm_wallet_watch as w
    import sniper.services.memecoin_evm_onchain_watch as ow
    from sniper.execution.evm_relay_quote import RELAY_APPROVAL_PROXY, RELAY_ROUTER
    from sniper.persistence.db_core import get_readonly_connection
    w._configure("4663")
    with contextlib.closing(get_readonly_connection(str(w.DB))) as c:
        c.row_factory = sqlite3.Row
        watched = w._verified_addresses(c)
        items = misses(c, a.since, watched)
    windows = watcher_windows(WATCHDOG_LOG.read_text(errors="replace").splitlines())
    arrivals = arrival_outcomes(WATCHER_LOG.read_text(errors="replace").splitlines())
    far = time.monotonic() + 1e9
    for contract in (RELAY_APPROVAL_PROXY, RELAY_ROUTER, ow.KYBER_ROUTER_4663):
        ow._RELAY_CODE_CACHE[("4663", contract)] = far
    chain, dec = Chain(), {}

    def decimals(mint):
        if mint not in dec:
            dec[mint] = int(chain("eth_call", [{"to": mint, "data": "0x313ce567"}, "latest"]), 16)
        return dec[mint]

    print("watched 4663 addresses %d; misses since %s: %d" % (len(watched), a.since, len(items)))
    counts = {}
    for it in items:
        to_watched = decoded = why = None
        detail = ""
        if it["tx"] and not a.dry and up_at(windows, it["t0"]) and it["trader"] in watched:
            tx = chain("eth_getTransactionByHash", [it["tx"]])
            rc = chain("eth_getTransactionReceipt", [it["tx"]])
            to_watched = any(len(lg.get("topics") or []) == 3 and lg["topics"][0] == ow.TRANSFER_TOPIC
                             and ow._topic_address(lg["topics"][2]) == it["trader"]
                             for lg in rc.get("logs") or [])
            buy = ow.direct_buy_from_receipt(rc, tx, it["trader"], weth=w.WETH, quote_usd=1.0,
                                             decimals_fn=decimals)
            path, ev = "direct", {}
            if not buy:
                path = "relay"
                buy = ow.read_relayer_purchase(rc, tx, it["trader"], chain="4663", evidence=ev,
                                               read_fn=chain, any_route=True, code_ttl_s=600)
            decoded, why = bool(buy), ev.get("reason")
            detail = "block %d tx.to %s path %s" % (int(rc["blockNumber"], 16), str(tx.get("to"))[:12], path)
        cause = classify(it, windows=windows, arrivals=arrivals, watched=watched,
                         receipt_to_watched=to_watched, decoded=decoded, why=why)
        counts[cause] = counts.get(cause, 0) + 1
        print("%s %-16s %s tx %s  %-24s %s" % (it["t0"].isoformat()[11:19], (it["handle"] or "")[:16],
                                             it["mint"][:10], (it["tx"] or it["origin"])[:14], cause, detail),
              flush=True)
    print("causes %s; chain calls %d" % (json.dumps(counts, sort_keys=True), chain.calls))


def selftest():
    lines = ["2026-09-28T06:37:04Z launched EVM on-chain watcher chain=4663 pid 1",
             "2026-09-28T06:43:04Z on-chain watcher chain=4663 is from a previous release; stopping",
             "2026-09-28T06:43:06Z launched EVM on-chain watcher chain=4663 pid 2",
             "2026-09-28T06:44:00Z launched EVM on-chain watcher chain=56 pid 3"]
    win = watcher_windows(lines)
    assert len(win) == 2 and up_at(win, utc("2026-09-28T06:40:00Z"))
    assert not up_at(win, utc("2026-09-28T06:43:05Z")) and not up_at(win, utc("2026-09-28T06:43:10Z"))
    assert up_at(win, utc("2026-09-28T06:43:17Z"))
    arr = arrival_outcomes([
        "2026-09-28T11:56:31.4+00:00 [onchain-evm] transfer 0x29225b3297 arrival=x outcome=no_buy retries=0",
        "[onchain-evm] detection 0xffbff3892b arrival=a decoded_at=b"])
    assert arr == {"0x29225b3297": ["no_buy"], "0xffbff3892b": ["detected"]}
    base = {"tx": "0x29225b32979a" + "0" * 52, "trader": "0xaa", "t0": utc("2026-09-28T11:56:31Z")}
    kw = dict(windows=win, arrivals=arr, watched={"0xaa": 1}, receipt_to_watched=True, why=None)
    assert classify(base, decoded=True, **kw) == "arrival:no_buy"
    assert classify(base, decoded=False, **kw) == "decoder:no_buy"
    assert classify(dict(base, tx=None), decoded=None, **kw) == "no_tx_hash"
    assert classify(dict(base, trader="0xbb"), decoded=True, **kw) == "not_watched"
    assert classify(dict(base, t0=utc("2026-09-28T06:43:05Z")), decoded=True, **kw) == "restart_gap"
    early = dict(base, tx="0x" + "1" * 64, t0=utc("2026-09-28T07:00:00Z"))
    assert classify(early, decoded=True, **kw) == "silent_pre_fix"
    assert classify(early, decoded=True, **dict(kw, receipt_to_watched=False)) == "no_transfer"
    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        selftest()
    else:
        main()

#!/usr/bin/env python3
"""Who would we copy on Ink (57073), and where do they and the Ink meme market trade?

Pre-registered in HANDOFF 2026-09-29 ~20:45Z (Gamma; Alpha, for the Ink funded runner plan). Public
Ink hosts only (rpc-gel / rpc-qnd.inkonchain.com, alternated, paced), never a funded host. Window: the
last 604,800 blocks (~7 days at 1 s). Read-only on the ledger (the proven EVM addresses).

  addresses  ERC20 Transfer logs to (topic2) or from (topic1) every CONFIRMED EVM address; per address
             tokens, txs, buys/sells (net non-quote token in = buy, out = sell) and the WETH/USDC leg
  venues     the Swap-event emitters in those txs' receipts, classed by event topic and pool.factory()
  market     every 7th 1000-block chunk (x7 to scale): Swap logs per venue class, distinct pools, and
             the WETH-pair share / distinct tokens over the top pools by swap count

    python3 scripts/research/ink_copy_universe.py [--out logs/acceptance/ink_copy_universe.json]
    python3 scripts/research/ink_copy_universe.py --selftest
"""
import argparse
import collections
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LEDGER = "file:%s?mode=ro" % (REPO / "sniper_tracker.db")
HOSTS = ("https://rpc-gel.inkonchain.com", "https://rpc-qnd.inkonchain.com")
PACE_S, CHUNK, WINDOW, SAMPLE_EVERY, TOP_POOLS = 0.3, 1000, 604_800, 7, 150
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SWAP_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SWAP_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
SWAP_VELO = "0xb3e2773606abfd36b5bd91394b3a54d1398336c65005baf7bf7a05efeffaf75b"
SWAP_V4 = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
UNI_V3_FACTORY = "0x640887a9ba3a9c53ed27d0f7e8246a4f933f3424"
UNI_V2_FACTORY = "0xfe57a6ba1951f69ae2ed4abe23e0f095df500c04"
POOL_MANAGER = "0x360e68faccca8ca495c1b759fd9eee466db9fb32"
WETH = "0x4200000000000000000000000000000000000006"
QUOTES = {WETH: 18, "0x2d270e6886d130d724215a266106e6832161eaed": 6, "0xf1815bd50389c46847f0bda824ec8da914045d14": 6}
# rpc-qnd's Cloudflare answers 403 "error code: 1010" to Python's default User-Agent (2026-09-29).
UA = "Mozilla/5.0 (sniper-research)"
SEL = {"factory": "0xc45a0155", "token0": "0x0dfe1681", "token1": "0xd21220a7"}

_state = {"i": 0, "last": 0.0, "calls": 0, "r429": 0}


def rpc(method, params):
    """One JSON-RPC call on the public Ink hosts, alternated and paced; 429 backs off and retries."""
    for attempt in range(6):
        wait = _state["last"] + PACE_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        url = HOSTS[_state["i"] % len(HOSTS)]
        _state["i"] += 1
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        try:
            out = json.loads(urllib.request.urlopen(urllib.request.Request(
                url, body, {"Content-Type": "application/json", "User-Agent": UA}), timeout=30).read())
        except urllib.error.HTTPError as e:
            _state["last"] = time.monotonic()
            if e.code == 429:
                _state["r429"] += 1
                time.sleep(2 ** attempt)
                continue
            raise
        finally:
            _state["last"] = time.monotonic()
            _state["calls"] += 1
        if "error" in out:
            if attempt < 5 and "limit" in str(out["error"]).lower():
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("%s: %s" % (method, out["error"]))
        return out.get("result")
    raise RuntimeError("%s: gave up after 429s" % method)


def topic_addr(a):
    return "0x" + "0" * 24 + a.lower()[2:]


def addr_of(topic):
    return "0x" + topic[-40:].lower()


def addresses():
    with sqlite3.connect(LEDGER, uri=True, timeout=30) as c:
        return sorted({str(r[0]).lower() for r in c.execute(
            "SELECT address FROM axiom_verified_addresses WHERE verify_status='axiom_confirmed' "
            "AND network_id IN ('1','56','4663','8453','42161','143','10','57073')")})


def flows(receipt, who):
    """{token: net atomic into `who`} over the receipt's ERC20 Transfer logs."""
    out = collections.Counter()
    for log in receipt.get("logs") or []:
        t = log.get("topics") or []
        if len(t) == 3 and t[0] == TRANSFER and log.get("data") not in (None, "0x"):
            amt = int(log["data"], 16)
            tok = log["address"].lower()
            if addr_of(t[2]) == who:
                out[tok] += amt
            if addr_of(t[1]) == who:
                out[tok] -= amt
    return out


def side_of(net):
    """buy / sell / other from one address's net token flows in a tx."""
    coins = {t: v for t, v in net.items() if t not in QUOTES and v}
    if not coins:
        return "other"
    return "buy" if any(v > 0 for v in coins.values()) and not any(v < 0 for v in coins.values()) else (
        "sell" if all(v < 0 for v in coins.values()) else "mixed")


def quote_leg(receipt, pools):
    """WETH-equivalent of the quote tokens that moved into or out of the tx's swap pools."""
    eth = 0.0
    for log in receipt.get("logs") or []:
        t = log.get("topics") or []
        tok = log.get("address", "").lower()
        if len(t) == 3 and t[0] == TRANSFER and tok in QUOTES and log.get("data") not in (None, "0x"):
            if addr_of(t[1]) in pools or addr_of(t[2]) in pools:
                amt = int(log["data"], 16) / 10 ** QUOTES[tok]
                eth = max(eth, amt if tok == WETH else 0.0)
    return eth


def venue_class(emitter, topic, meta):
    if topic == SWAP_V4:
        return "uniswap_v4" if emitter == POOL_MANAGER else "v4_other"
    fac = (meta.get(emitter) or {}).get("factory")
    if topic == SWAP_V3:
        return "uniswap_v3" if fac == UNI_V3_FACTORY else "v3_other(slipstream/fork)"
    if topic == SWAP_V2:
        return "uniswap_v2" if fac == UNI_V2_FACTORY else "v2_other"
    if topic == SWAP_VELO:
        return "velodrome_classic"
    return "other"


def call(to, sel):
    try:
        r = rpc("eth_call", [{"to": to, "data": sel}, "latest"])
    except Exception:
        return None
    return ("0x" + r[-40:]).lower() if r and len(r) >= 66 else None


def pool_meta(pools, meta):
    for p in pools:
        if p in meta or p == POOL_MANAGER:
            continue
        meta[p] = {k: call(p, s) for k, s in SEL.items()}


def run(args):
    head = int(rpc("eth_blockNumber", []), 16)
    lo = head - WINDOW
    t_head = int(rpc("eth_getBlockByNumber", [hex(head), False])["timestamp"], 16)
    t_lo = int(rpc("eth_getBlockByNumber", [hex(lo), False])["timestamp"], 16)
    print("window blocks %d..%d = %.2f days" % (lo, head, (t_head - t_lo) / 86400.0), flush=True)
    addrs = addresses()
    print("confirmed EVM addresses: %d" % len(addrs), flush=True)
    topics = [topic_addr(a) for a in addrs]
    logs = []
    for start in range(lo, head + 1, CHUNK):
        end = min(head, start + CHUNK - 1)
        for filt in ([TRANSFER, None, topics], [TRANSFER, topics]):
            logs.extend(rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "topics": filt}]) or [])
    print("address transfer logs: %d (calls %d, 429s %d)" % (len(logs), _state["calls"], _state["r429"]), flush=True)
    per = collections.defaultdict(lambda: {"txs": set(), "tokens": set()})
    aset = set(addrs)
    for log in logs:
        t = log["topics"]
        for who in {addr_of(t[1]), addr_of(t[2])} & aset:
            per[who]["txs"].add(log["transactionHash"])
            per[who]["tokens"].add(log["address"].lower())
    meta, venue_by_addr, sides, vol = {}, collections.defaultdict(collections.Counter), \
        collections.defaultdict(collections.Counter), collections.Counter()
    for who, d in per.items():
        for tx in sorted(d["txs"]):
            rc = rpc("eth_getTransactionReceipt", [tx]) or {}
            swaps = [(l["address"].lower(), l["topics"][0]) for l in rc.get("logs") or []
                     if l.get("topics") and l["topics"][0] in (SWAP_V3, SWAP_V2, SWAP_VELO, SWAP_V4)]
            pool_meta({e for e, _t in swaps}, meta)
            s = side_of(flows(rc, who))
            sides[who][s] += 1
            for e, t in swaps:
                venue_by_addr[who][venue_class(e, t, meta)] += 1
            if s in ("buy", "sell"):
                vol[who] += quote_leg(rc, {e for e, _t in swaps})
    # market sample
    mk = collections.Counter()
    pools = collections.Counter()
    n_chunks = 0
    for i, start in enumerate(range(lo, head + 1, CHUNK)):
        if i % SAMPLE_EVERY:
            continue
        n_chunks += 1
        end = min(head, start + CHUNK - 1)
        for topic in (SWAP_V3, SWAP_V2, SWAP_VELO, SWAP_V4):
            for l in rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "topics": [topic]}]) or []:
                key = (l["address"].lower(), topic, l["topics"][1] if topic == SWAP_V4 else None)
                pools[key] += 1
    top = [k for k, _n in pools.most_common(TOP_POOLS)]
    pool_meta({k[0] for k in top}, meta)
    weth_pairs, tokens = collections.Counter(), collections.defaultdict(set)
    for key, n in pools.items():
        cls = venue_class(key[0], key[1], meta)
        mk[cls] += n
        m = meta.get(key[0]) or {}
        if key in top and m.get("token0"):
            pair = {m.get("token0"), m.get("token1")}
            if WETH in pair:
                weth_pairs[cls] += n
                tokens[cls] |= pair - {WETH}
    out = {"window": [lo, head], "days": (t_head - t_lo) / 86400.0, "addresses": len(addrs),
           "active": {w: {"txs": len(d["txs"]), "tokens": len(d["tokens"]), "sides": dict(sides[w]),
                          "weth": round(vol[w], 4), "venues": dict(venue_by_addr[w])} for w, d in per.items()},
           "market_sample_chunks": n_chunks, "market_swaps_x7": {k: v * SAMPLE_EVERY for k, v in mk.items()},
           "market_pools": len(pools), "top_pools_weth_pair_swaps_x7": {k: v * SAMPLE_EVERY for k, v in weth_pairs.items()},
           "top_pools_distinct_tokens": {k: len(v) for k, v in tokens.items()},
           "rpc_calls": _state["calls"], "r429": _state["r429"]}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


def selftest():
    a, b = "0x" + "11" * 20, "0x" + "22" * 20
    coin = "0x" + "33" * 20
    rc = {"logs": [
        {"address": coin, "topics": [TRANSFER, topic_addr(b), topic_addr(a)], "data": hex(500)},
        {"address": WETH, "topics": [TRANSFER, topic_addr(a), topic_addr(b)], "data": hex(10 ** 17)}]}
    assert flows(rc, a) == {coin: 500, WETH: -10 ** 17}
    assert side_of(flows(rc, a)) == "buy" and side_of({coin: -5}) == "sell" and side_of({WETH: 1}) == "other"
    assert abs(quote_leg(rc, {b}) - 0.1) < 1e-12
    assert venue_class(POOL_MANAGER, SWAP_V4, {}) == "uniswap_v4"
    assert venue_class("0xp", SWAP_V3, {"0xp": {"factory": UNI_V3_FACTORY}}) == "uniswap_v3"
    assert venue_class("0xp", SWAP_V3, {"0xp": {"factory": "0xother"}}).startswith("v3_other")
    assert venue_class("0xp", SWAP_V2, {"0xp": {"factory": UNI_V2_FACTORY}}) == "uniswap_v2"
    assert venue_class("0xp", SWAP_VELO, {}) == "velodrome_classic"
    assert addr_of(topic_addr(a)) == a
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=str(REPO / "logs" / "acceptance" / "ink_copy_universe.json"))
    a = ap.parse_args()
    return selftest() if a.selftest else run(a)


if __name__ == "__main__":
    main()

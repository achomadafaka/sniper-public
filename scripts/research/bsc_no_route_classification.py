#!/usr/bin/env python3
"""Alpha's task, 2026-09-08: how big is the chain-56 no_route problem, really.

Chain 56 became routable at 11:00 that day; every attempt that reached a quote (27 rows in
memecoin_entry_attempts, 5 distinct mints after collapsing retries) refused with
'no quote (no_route: no route on v3/v2/pons_v2 ...)'. Alpha found one confirmed false refusal (a
seam bug in the V2 quoter reading a failed RPC read as "no path") and needs to know how many of
the other misses are the SAME bug versus genuinely unroutable, before deciding how much the fix
is worth. Classifies each mint into exactly one bucket:

  1. NO ROUTABLE PAIR EVER    -- no token/WBNB or token/USDT pair at V2 or V3, ever. Correct refusal.
  2. POOL CAME LATER          -- a routable pair exists but was created AFTER our refusal.
  3. POOL EXISTED, WE MISSED IT -- routable pair created before our refusal. The false refusals.

GROUND TRUTH, per Alpha's own instrument warnings (DexScreener's dexId/labels lied about the
factory for at least one token this session): every pair/pool address here comes from calling
getPair()/getPool() directly on the canonical factories, never from a third-party aggregator.
  - PancakeSwap V2 factory: 0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73 (Alpha's own instruction).
  - PancakeSwap V3 factory: read live from the configured V3 QuoterV2's own factory() call this
    session (0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865) -- not typed in from memory.
  - pons_v2 has no chain-56 config anywhere in sniper/settings/memecoin.py -- it is a venue name
    the route-decision code always tries, always fails to configure for this chain, and always
    contributes "no route" trivially. Not checked separately; V2 and V3 are chain 56's real venues.
  - "Routable" means paired with WBNB or USDT (this codebase's only two connectors on chain 56)
    -- a pair against a third memecoin is real liquidity but not a route we can fund, and belongs
    in bucket 1, exactly as Alpha specified for the GMEB case.

Pair/pool CREATION TIME: two approaches were tried and rejected before this one.
  1. eth_getCode binary search -- WRONG. This public BSC node's state (eth_getCode/eth_call at a
     past block) is only servable for roughly the last hour ("missing trie node" past that), so
     every historical probe silently read as "no code yet" and the search converged on "now"
     regardless of the real creation time.
  2. eth_getLogs on the factory's PairCreated/PoolCreated event -- ALSO unusable. This endpoint
     refuses eth_getLogs outright with "limit exceeded", even for a single block with zero
     filters -- confirmed by testing a bare, unfiltered one-block call directly.
Both discovered live this run, not assumed. Landed on DexScreener's `pairCreatedAt` for the
timestamp ONLY, cross-checked against the ground-truth pair/pool address from step 1 above (never
trusted on its own -- Alpha's own GMEB case is exactly why: a DexScreener pair entry can be
labelled 'pancakeswap v2' for a pair that getPair() proves doesn't exist against our connectors at
all). Verified against Alpha's own confirmed example (Taraxacum) before trusting this on the rest:
DexScreener's pairCreatedAt for the address that matched our getPair() result gave a 20.15-hour
gap against Alpha's own "created ~20 hours earlier" — same number, independently reproduced.

Read-only. Public BSC RPC only (cfg.evm_rpc_for_chain("56"), not funded quota). DexScreener's
public token-pairs endpoint, paced. No DB writes.
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

DB = REPO / "sniper_tracker.db"
V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
CALL_PAUSE_S = 0.3


def _rpc(rpc, method, params, tries=3):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for _ in range(tries):
        try:
            req = urllib.request.Request(rpc, data=body, headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                return None, d["error"]
            return d.get("result"), None
        except Exception:
            time.sleep(1.0)
    return None, "failed"


def eth_call(rpc, to, data, block="latest"):
    time.sleep(CALL_PAUSE_S)
    res, err = _rpc(rpc, "eth_call", [{"to": to, "data": data}, block])
    return res, err


def pad_addr(addr):
    return addr.lower().replace("0x", "").rjust(64, "0")


def v2_pair(rpc, factory, a, b):
    data = "0xe6a43905" + pad_addr(a) + pad_addr(b)   # getPair(address,address)
    res, err = eth_call(rpc, factory, data)
    if err or not res:
        return None
    addr = "0x" + res[-40:]
    return None if int(addr, 16) == 0 else addr


def v3_pool(rpc, factory, a, b, fee):
    data = "0x1698ee82" + pad_addr(a) + pad_addr(b) + format(fee, "064x")   # getPool(address,address,uint24)
    res, err = eth_call(rpc, factory, data)
    if err or not res:
        return None
    addr = "0x" + res[-40:]
    return None if int(addr, 16) == 0 else addr


DEXSCREENER_PAUSE_S = 1.2


def dexscreener_pairs(mint):
    """Raw DexScreener pairs list for one token. NEVER trust dexId/labels from this for factory
    identity (Alpha's own GMEB case: a pair labelled 'pancakeswap v2' whose getPair() came back
    the zero address). Used ONLY as a source of pairCreatedAt, and only after the caller matches
    the pair address against an independently-verified ground-truth getPair()/getPool() result."""
    time.sleep(DEXSCREENER_PAUSE_S)
    try:
        req = urllib.request.Request(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            headers={"User-Agent": "research/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return (json.loads(r.read().decode()) or {}).get("pairs") or []
    except Exception:
        return []


def dexscreener_match(pairs, ground_truth_addr):
    """(created_epoch_s, liquidity_usd) for the pair whose ADDRESS matches ground_truth_addr, or
    (None, None) if DexScreener never indexed this exact pair (a real limitation, not a zero)."""
    for p in pairs:
        if (p.get("pairAddress") or "").lower() == ground_truth_addr.lower():
            ts_ms = p.get("pairCreatedAt")
            created = int(ts_ms) // 1000 if ts_ms else None
            liq = (p.get("liquidity") or {}).get("usd")
            return created, liq
    return None, None


def v2_reserves_usd(rpc, pair, token, wbnb, usdt):
    data0, err0 = eth_call(rpc, pair, "0x0dfe1681")   # token0()
    data1, err1 = eth_call(rpc, pair, "0xd21220a7")   # token1()
    if err0 or err1:
        return None
    token0 = "0x" + data0[-40:]
    reserves, err = eth_call(rpc, pair, "0x0902f1ac")   # getReserves()
    if err or not reserves:
        return None
    r0 = int(reserves[2:66], 16)
    r1 = int(reserves[66:130], 16)
    is_token0 = token0.lower() == token.lower()
    other_reserve = r1 if is_token0 else r0
    other_is_usdt = (data1 if is_token0 else data0)[-40:].lower() == usdt[2:].lower()
    return other_reserve / 1e18   # USDT and WBNB are both 18dec on BSC; WBNB side is BNB not USD


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2026-09-01")
    args = ap.parse_args()

    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_wallet import CHAIN_QUOTE, CHAIN_STABLE
    from sniper.settings.memecoin import MEMECOIN_EVM_V3_FEE_TIERS_BY_CHAIN

    rpc = cfg.evm_rpc_for_chain("56")
    wbnb = CHAIN_QUOTE["56"]["token"]
    usdt = CHAIN_STABLE["56"]
    v3_fees = MEMECOIN_EVM_V3_FEE_TIERS_BY_CHAIN.get("56")

    v3_factory, err = eth_call(rpc, cfg.MEMECOIN_EVM_V3_QUOTER["56"], "0xc45a0155")  # factory()
    if err:
        sys.exit(f"REFUSE: could not read the V3 factory address live: {err}")
    v3_factory = "0x" + v3_factory[-40:]
    print(f"WBNB={wbnb}  USDT={usdt}  V2_factory={V2_FACTORY}  V3_factory={v3_factory} "
          f"(read live)  V3 fee tiers={v3_fees}", flush=True)

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    mints = c.execute(
        "SELECT mint, MIN(decision_started_at) first_refusal, "
        "GROUP_CONCAT(DISTINCT source_handle) handles, COUNT(*) n_attempts "
        "FROM memecoin_entry_attempts WHERE network_id='56' AND decision_started_at>=? "
        "AND terminal_reason LIKE '%no_route%' GROUP BY mint ORDER BY first_refusal",
        (args.since,)).fetchall()
    print(f"distinct mints refused no_route since {args.since}: {len(mints)}\n", flush=True)

    results = []
    for m in mints:
        mint, first_refusal, handles = m["mint"], m["first_refusal"], m["handles"]
        refusal_epoch = int(datetime.fromisoformat(first_refusal.replace("Z", "+00:00")).timestamp())
        print(f"=== {mint}  first refused {first_refusal} ({handles}, {m['n_attempts']} attempts) ===",
              flush=True)

        candidates = []   # (venue, pair_addr, quote_side)
        for conn_addr, conn_name in ((wbnb, "WBNB"), (usdt, "USDT")):
            p = v2_pair(rpc, V2_FACTORY, mint, conn_addr)
            if p:
                candidates.append(("v2", p, conn_name))
            for fee in v3_fees:
                p3 = v3_pool(rpc, v3_factory, mint, conn_addr, fee)
                if p3:
                    candidates.append((f"v3:{fee}", p3, conn_name))

        if not candidates:
            print("  NO routable pair/pool at V2 or V3 against WBNB or USDT.")
            results.append({"mint": mint, "bucket": 1, "handles": handles})
            continue

        ds_pairs = dexscreener_pairs(mint)
        earliest = None
        earliest_liq = None
        any_unknown = False
        for venue, addr, conn_name in candidates:
            dep_epoch, ds_liq = dexscreener_match(ds_pairs, addr)
            onchain_reserve = v2_reserves_usd(rpc, addr, mint, wbnb, usdt) if venue == "v2" else None
            reserve_unit = conn_name if venue == "v2" else None
            if dep_epoch is None:
                any_unknown = True
                dep_iso = "UNKNOWN (DexScreener never indexed this exact ground-truth pair address)"
            else:
                dep_iso = datetime.fromtimestamp(dep_epoch, tz=timezone.utc).isoformat()
                if earliest is None or dep_epoch < earliest:
                    earliest, earliest_liq = dep_epoch, ds_liq
            liq_str = f"liq(DexScreener)=${ds_liq:,.2f}" if ds_liq is not None else ""
            reserve_str = (f" reserve(on-chain)={onchain_reserve:.4f} {reserve_unit}"
                           if onchain_reserve is not None else "")
            print(f"  {venue:6s} vs {conn_name:4s} pair={addr}  created={dep_iso}  "
                  f"{liq_str}{reserve_str}")

        if earliest is None:
            print("  -> creation time UNKNOWN for every routable candidate -- not classified, "
                  "flagged for manual check\n", flush=True)
            results.append({"mint": mint, "bucket": None, "handles": handles,
                             "note": "routable pair confirmed on-chain, creation time unknown"})
            continue

        gap_min = (earliest - refusal_epoch) / 60.0
        bucket = 2 if earliest > refusal_epoch else 3
        caveat = "  [only a LOWER bound -- another candidate's creation time was unknown]" if any_unknown else ""
        print(f"  -> earliest KNOWN routable pair creation vs first refusal: gap={gap_min:.1f} min "
              f"-> BUCKET {bucket}{caveat}\n", flush=True)
        results.append({"mint": mint, "bucket": bucket, "gap_min": gap_min, "handles": handles,
                         "first_refusal": first_refusal, "any_unknown": any_unknown,
                         "liq_usd": earliest_liq})

    print("=== SUMMARY ===")
    for b in (1, 2, 3):
        rows = [r for r in results if r["bucket"] == b]
        print(f"bucket {b}: {len(rows)} mint(s)")
        for r in rows:
            extra = f"  gap={r['gap_min']:.1f}min" if r.get("gap_min") is not None else ""
            liq = f"  liq=${r['liq_usd']:,.2f}" if r.get("liq_usd") is not None else ""
            flag = "  [lower bound]" if r.get("any_unknown") else ""
            print(f"    {r['mint']}  ({r['handles']}){extra}{liq}{flag}")
    unclassified = [r for r in results if r["bucket"] is None]
    if unclassified:
        print(f"unclassified (routable pair confirmed, creation time unknown): {len(unclassified)}")
        for r in unclassified:
            print(f"    {r['mint']}  ({r['handles']})")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Timing and simulation harness for sniper/execution/pump_curve_buy.py. Never sends a transaction.

    --timing N       TradeEvent log line in, signed bytes out, N times, with a prefetched Jupiter
                     USDC->SOL leg and a cached blockhash. Signs with a THROWAWAY keypair (never the
                     custody key); pubkeys are 32 bytes either way, so the size is the live size.
    --simulate K     K live SOL-quoted curve buys from the pump program's recent transactions,
                     each built for the custody PUBKEY (public; no key is loaded) and simulated with
                     sigVerify false, replaceRecentBlockhash true.

Reads only the research Helius key (MEMECOIN_RESEARCH_HELIUS_RPC_URLS) and Jupiter under the
CAPTURE key role. Both are research lanes; the funded custody RPC and funded Jupiter key are not used.
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sniper.util.envfiles import load_env_files  # noqa: E402
from sniper.util.state_paths import state_root  # noqa: E402

load_env_files(str(state_root()))

from sniper.execution import pump_curve_buy as pcb  # noqa: E402
from sniper.execution.pump_native_exit import PUMP_PROGRAM, _pda  # noqa: E402

CUSTODY_PUBKEY = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
_URLS = [u.strip() for u in (os.getenv("MEMECOIN_RESEARCH_HELIUS_RPC_URLS")
                             or os.getenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL") or "").split(",") if u.strip()]
RPC_CALLS = [0]


def rpc(method, params):
    if not _URLS:
        raise SystemExit("no research Helius URL configured")
    RPC_CALLS[0] += 1
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(_URLS[0], data=body, headers={
        "content-type": "application/json", "user-agent": "curl/8.5.0"})
    out = json.loads(urllib.request.urlopen(req, timeout=20).read())
    if "error" in out:                       # an error is an error, never a null result
        raise RuntimeError("%s: %s" % (method, out["error"]))
    return out["result"]


def jup_call(method, path, body=None):
    from sniper.execution.solana_real_seams import ROLE_CAPTURE, _jup_call, jupiter_role
    with jupiter_role(ROLE_CAPTURE):
        return _jup_call(method, path, body=body)


def live_curve_buys(k, max_tx=40):
    """Up to k recent SOL-quoted curve buys: (event, token_program, signature)."""
    import base58
    sigs = rpc("getSignaturesForAddress", [PUMP_PROGRAM, {"limit": 100, "commitment": "confirmed"}])
    out, seen_mints = [], set()
    for s in sigs[:max_tx]:
        if s.get("err") or len(out) >= k:
            continue
        try:
            tx = rpc("getTransaction", [s["signature"], {
                "encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
        except RuntimeError as exc:
            print("skip %s: %s" % (s["signature"][:10], str(exc)[:80]))
            continue
        time.sleep(0.3)
        logs = (tx or {}).get("meta", {}).get("logMessages") or []
        for line in logs:
            if not line.startswith("Program data: vdt/"):
                continue
            try:
                ev = pcb.parse_trade_event(line)
            except ValueError:
                continue
            if (ev["is_buy"] and ev["quote_mint"] in pcb.SOL_QUOTE_MINTS
                    and ev["real_token_reserves"] > 0 and ev["mint"] not in seen_mints):
                seen_mints.add(ev["mint"])
                out.append((ev, pcb.token_program_from_logs(logs), s["signature"], line))
                break
    del base58
    return out


def leg_study(events, buyback, blockhash, usdc_atomic, a):
    """Where does an aged leg fail? Per trial: fetch a leg, wait --leg-age, build and simulate. A
    failure is attributed by instruction index (two compute-budget instructions, then the leg)."""
    from solders.null_signer import NullSigner
    from solders.pubkey import Pubkey
    user = Pubkey.from_string(CUSTODY_PUBKEY)
    levels = [int(x) for x in a.bps.split(",")]
    tally = {b: {"ok": 0, "leg": 0, "curve": 0, "other": 0, "no_leg": 0, "venues": {}} for b in levels}
    alt_cache = {}
    for trial in range(a.leg_study):
        for bps in levels:
            ev, tp, _sig, _line = events[trial % len(events)]
            leg = pcb.prefetch_usdc_sol_leg(usdc_atomic, bps, user=user, jup_call=jup_call,
                                            rpc=rpc, alt_cache=alt_cache)
            if leg is None:
                tally[bps]["no_leg"] += 1
                continue
            venue = ",".join(r["swapInfo"].get("label", "?") for r in leg["quote"].get("routePlan") or ())
            time.sleep(a.leg_age)
            built = pcb.build_curve_buy(
                ev, leg, user=user, token_program=tp, buyback_fee_recipient=buyback,
                blockhash=blockhash, slippage_pct=a.curve_slippage_pct, signers=[NullSigner(user)])
            value = pcb.simulate_curve_buy(built["tx"], rpc=rpc) or {}
            err = value.get("err")
            n_leg = len(leg["ixs"])
            if err is None:
                kind = "ok"
            elif isinstance(err, dict) and "InstructionError" in err:
                idx = err["InstructionError"][0]
                kind = "leg" if 2 <= idx < 2 + n_leg else ("curve" if idx >= 2 + n_leg else "other")
            else:
                kind = "other"
            tally[bps][kind] += 1
            v = tally[bps]["venues"].setdefault(venue, {"ok": 0, "fail": 0})
            v["ok" if kind == "ok" else "fail"] += 1
            print("trial %d bps %d venue %s -> %s %s units %s" % (
                trial, bps, venue, kind, err if err else "", value.get("unitsConsumed")))
    print("LEG STUDY age %.1fs: %s" % (a.leg_age, json.dumps(tally)))


def live_pumpswap_buys(k, max_tx=40):
    """Up to k recent PumpSwap buys: (event dict, raw BuyEvent line, signature), distinct pools."""
    from sniper.execution import pumpswap_buy as ps
    sigs = rpc("getSignaturesForAddress", [ps.AMM_PROGRAM, {"limit": 100, "commitment": "confirmed"}])
    out, seen = [], set()
    for s in sigs[:max_tx]:
        if s.get("err") or len(out) >= k:
            continue
        try:
            tx = rpc("getTransaction", [s["signature"], {
                "encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])
        except RuntimeError as exc:
            print("skip %s: %s" % (s["signature"][:10], str(exc)[:80]))
            continue
        time.sleep(0.3)
        for line in (tx or {}).get("meta", {}).get("logMessages") or []:
            if not line.startswith("Program data: Z/RSHyz1d3"):
                continue
            try:
                ev = ps.parse_buy_event(line)
            except ValueError:
                continue
            if ev["pool"] not in seen:
                seen.add(ev["pool"])
                out.append((ev, line, s["signature"]))
                break
    return out


def pumpswap_sims(k, usdc):
    """Full adapter path on live PumpSwap buys: learn pool, warm the leg, build, simulate at
    processed. SOL/USD for the price cap comes from the warmed leg's own quote."""
    from types import SimpleNamespace
    from solders.null_signer import NullSigner
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    from sniper.execution import pumpswap_buy as ps
    user = Pubkey.from_string(CUSTODY_PUBKEY)
    b = ps.SlotLanePumpSwapBuilder(user=CUSTODY_PUBKEY, jup_call=jup_call, rpc=rpc)
    bh = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
    snap = SimpleNamespace(blockhash=bh["blockhash"], last_valid_height=bh["lastValidBlockHeight"],
                           sol_usd=None)
    for ev, line, sig in live_pumpswap_buys(k * 2):
        buy = SimpleNamespace(pool=ev["pool"], trade_event_log=line)
        _msg, first_sight = b.build(buy, usdc, snap)        # first sight: pool_unknown, queued
        b.refresh(usdc)                                     # learns the pool, warms the wSOL leg
        if b._leg:
            snap.sol_usd = usdc / (b._leg["out_lamports"] / 1e9)
        facts = b.pools.get(ev["pool"])
        if facts is None or facts["quote_mint"] != pcb.WSOL_MINT:
            print("SKIP pool %s: %s" % (ev["pool"][:8], "unlearned" if facts is None else "quote " + facts["quote_mint"][:6]))
            continue
        msg, why = b.build(buy, usdc, snap)
        if msg is None:
            print("REFUSED pool %s: %s" % (ev["pool"][:8], why))
            continue
        tx = bytes(VersionedTransaction(msg, [NullSigner(user)]))
        v = pcb.simulate_curve_buy(tx, rpc=rpc) or {}
        logs = v.get("logs") or []
        print(json.dumps({"pool": ev["pool"][:10], "mint": facts["base_mint"][:10], "source_sig": sig[:12],
                          "first_sight": first_sight, "bytes": b.last_build["bytes"], "err": v.get("err"),
                          "units": v.get("unitsConsumed"),
                          "amm_ok": any(ps.AMM_PROGRAM + " success" in l for l in logs),
                          "min_tokens_out": b.last_build["min_tokens_out"],
                          "tail": [l for l in logs if "rror" in l or "failed" in l][-2:]}))
        k -= 1
        if k <= 0:
            break


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timing", type=int, default=0)
    ap.add_argument("--simulate", type=int, default=0)
    ap.add_argument("--usdc", type=float, default=1.0, help="clip in USDC")
    ap.add_argument("--leg-slippage-bps", type=int, default=50)
    ap.add_argument("--curve-slippage-pct", type=float, default=30.0)
    ap.add_argument("--save-frames", default="")
    ap.add_argument("--leg-study", type=int, default=0,
                    help="trials per leg slippage level, interleaved across levels")
    ap.add_argument("--bps", default="50,100,150")
    ap.add_argument("--leg-age", type=float, default=1.0, help="seconds between leg fetch and sim")
    ap.add_argument("--pumpswap", type=int, default=0,
                    help="simulate K live wSOL-quoted PumpSwap buys through SlotLanePumpSwapBuilder")
    a = ap.parse_args()
    from solders.keypair import Keypair
    from solders.null_signer import NullSigner
    from solders.pubkey import Pubkey

    glob = rpc("getAccountInfo", [str(_pda([b"global"], PUMP_PROGRAM)), {"encoding": "base64"}])["value"]
    buyback = pcb.buyback_fee_recipient_from_global(glob["data"][0])
    blockhash = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
    usdc_atomic = int(round(a.usdc * 1_000_000))
    alt_cache = {}
    events = live_curve_buys(max(1, a.simulate))
    if not events:
        raise SystemExit("no live SOL-quoted curve buy found")
    if a.save_frames:
        Path(a.save_frames).write_text(json.dumps([
            {"signature": s, "token_program": tp, "log_line": line} for _e, tp, s, line in events], indent=1))

    if a.timing:
        kp = Keypair()
        t0 = time.perf_counter()
        leg = pcb.prefetch_usdc_sol_leg(usdc_atomic, a.leg_slippage_bps, user=kp.pubkey(),
                                        jup_call=jup_call, rpc=rpc, alt_cache=alt_cache)
        print("leg prefetch %.0f ms (off the critical path), tables %d, min_out %d lamports"
              % ((time.perf_counter() - t0) * 1000, len(leg["tables"]), leg["min_out_lamports"]))
        ev, tp, sig, line = events[0]
        times, size = [], None
        for _ in range(a.timing):
            t = time.perf_counter()
            event = pcb.parse_trade_event(line)                     # the signal as it arrives
            built = pcb.build_curve_buy(event, leg, user=kp.pubkey(), token_program=tp,
                                        buyback_fee_recipient=buyback, blockhash=blockhash,
                                        slippage_pct=a.curve_slippage_pct, signers=[kp])
            times.append((time.perf_counter() - t) * 1000)
            size = built["bytes"]
        print("TIMING n=%d event->signed bytes p50 %.3f ms p90 %.3f ms max %.3f ms; tx %d bytes "
              "(packet 1232)" % (len(times), q(times, .5), q(times, .9), max(times), size))

    for ev, tp, sig, _line in events[:a.simulate]:
        user = Pubkey.from_string(CUSTODY_PUBKEY)
        leg = pcb.prefetch_usdc_sol_leg(usdc_atomic, a.leg_slippage_bps, user=user,
                                        jup_call=jup_call, rpc=rpc, alt_cache=alt_cache)
        if leg is None:
            print("SIM %s: no Jupiter leg" % ev["mint"])
            continue
        try:
            built = pcb.build_curve_buy(ev, leg, user=user, token_program=tp,
                                        buyback_fee_recipient=buyback, blockhash=blockhash,
                                        slippage_pct=a.curve_slippage_pct,
                                        signers=[NullSigner(user)])
        except ValueError as exc:
            print("SIM %s: refused before build: %s" % (ev["mint"], exc))
            continue
        value = pcb.simulate_curve_buy(built["tx"], rpc=rpc) or {}
        logs = value.get("logs") or []
        print(json.dumps({"mint": ev["mint"], "source_sig": sig[:16], "token_program": tp[:6],
                          "bytes": built["bytes"], "spend_lamports": built["spend_lamports"],
                          "min_tokens_out": built["min_tokens_out"], "err": value.get("err"),
                          "units": value.get("unitsConsumed"),
                          "pump_ok": any("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success" in l for l in logs),
                          "tail": [l for l in logs if "rror" in l or "failed" in l][-3:]}))
    if a.leg_study:
        leg_study(events, buyback, blockhash, usdc_atomic, a)
    if a.pumpswap:
        pumpswap_sims(a.pumpswap, a.usdc)
    print("research rpc calls:", RPC_CALLS[0])


if __name__ == "__main__":
    main()

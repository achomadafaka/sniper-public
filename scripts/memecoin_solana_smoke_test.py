#!/usr/bin/env python3
"""Blocker 7 — Solana leg smoke test: a $5 USDC->BONK->USDC round trip through the FULL real stack
(custody -> Jupiter v6 quote -> sign -> nonce/sig write-ahead -> send -> confirm -> chain-delta fill ->
exit ladder). This is the ARM GATE for the Solana leg — operator-run AFTER funding the wallet, and the
only end-to-end proof of the path (unit tests cover the branches, not the chain).

It exercises the production executor (build_real_solana_executor execute_fn / exit_fn) against a TEMP
DB (never the prod DB), so a pass means the real code path works — not a bespoke probe.

  --dry-run   balances + a live buy quote only (no broadcast) — validates creds/connectivity.
  --confirm   actually broadcast (SPENDS REAL FUNDS). Required for the round trip.

Env (via .env / .fomo.env / shell): MEMECOIN_SOLANA_KEYPAIR (funded, 0600), MEMECOIN_SOLANA_RPC_URL.
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sniper.util.envfiles import load_env_files
load_env_files(_ROOT)

from sniper.execution import solana_wallet as W
from sniper.execution.solana_real_seams import build_real_solana_executor, jupiter_entry_quote
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"   # a liquid Solana memecoin for the round trip


def _now():
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(description="Solana leg smoke test ($5 USDC->BONK->USDC round trip)")
    ap.add_argument("--mint", default=BONK, help="token to round-trip (default BONK)")
    ap.add_argument("--usd", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true", help="balances + buy quote only, no broadcast")
    ap.add_argument("--confirm", action="store_true", help="broadcast (SPENDS REAL FUNDS)")
    args = ap.parse_args()

    rpc = cfg.MEMECOIN_SOLANA_RPC_URL
    if not rpc:
        sys.exit("MEMECOIN_SOLANA_RPC_URL is unset — cannot reach the chain")
    if not os.path.exists(cfg.MEMECOIN_SOLANA_KEYPAIR):
        sys.exit(f"Solana keypair not found at {cfg.MEMECOIN_SOLANA_KEYPAIR} — fund + place it (0600) first")

    kp = W.load_keypair(cfg.MEMECOIN_SOLANA_KEYPAIR)
    addr = W.pubkey_str(kp)
    sol = W.sol_balance(addr, rpc)
    usdc = W.usdc_balance(addr, rpc)
    print(f"wallet   {addr}")
    print(f"rpc      {'set' if rpc else 'MISSING'}")
    print(f"balances SOL={sol:.6f}  USDC={usdc:.4f}")

    q = jupiter_entry_quote(args.mint, args.usd, http=None)
    if not q:
        sys.exit("FAIL: Jupiter buy quote returned nothing (check connectivity / mint / RPC).")
    print(f"buy quote OK: in {q.get('inAmount')} -> out {q.get('outAmount')}  "
          f"(priceImpact {q.get('priceImpactPct')})")

    if args.dry_run or not args.confirm:
        print("\nDRY RUN — no broadcast. Re-run with --confirm to spend $%.2f and complete the round trip."
              % args.usd)
        return

    if usdc < args.usd:
        sys.exit(f"FAIL: USDC balance {usdc:.4f} < trade size {args.usd} — fund the wallet.")
    if sol < cfg.MEMECOIN_GAS_ENTRY_SOL + cfg.MEMECOIN_GAS_EXIT_SOL:
        sys.exit(f"FAIL: SOL balance {sol:.6f} below entry+exit gas — fund SOL.")

    db_path = os.path.join(_ROOT, "data", "sol_smoke.sqlite")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    ex = build_real_solana_executor(keypair=kp, rpc_url=rpc)

    print("\n--- BUY (USDC -> token) via production execute_fn ---")
    pid = m.reserve_pending(conn, mint=args.mint, source_user_id="smoke", strategy="alpha_b",
                            notional=args.usd, reserved_at=_now(), network_id="solana")
    t0 = time.perf_counter()
    buy = ex["execute_fn"](conn, pid, args.mint, args.usd, _now())
    print(f"buy result: {buy}  ({(time.perf_counter()-t0)*1000:.0f}ms)")
    if buy.get("status") != "filled":
        sys.exit(f"FAIL: buy did not fill (status={buy.get('status')}). Inspect above.")

    print("\n--- SELL (token -> USDC) via production exit_fn ---")
    pos = m.get_position(conn, buy["position_id"])
    t1 = time.perf_counter()
    sell = ex["exit_fn"](conn, pos, "smoke", _now())
    print(f"sell result: {sell}  ({(time.perf_counter()-t1)*1000:.0f}ms)")

    usdc_after = W.usdc_balance(addr, rpc)
    print(f"\nround-trip USDC: {usdc:.4f} -> {usdc_after:.4f}  (cost ${usdc - usdc_after:.4f} on ${args.usd})")
    if sell.get("status") == "closed":
        print("SMOKE TEST PASSED — the full Solana buy+sell path works. Arm the Solana leg per the runbook.")
    else:
        print(f"SELL not clean (status={sell.get('status')}). Do NOT arm until the exit path is proven.")


if __name__ == "__main__":
    main()
